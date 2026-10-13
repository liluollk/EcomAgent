"""``set_listing_price`` 两阶段写测试(P1 Task 6;设计 §4.3、§5.2、§6)。

覆盖:
- Tool Contract:输入 Schema 只暴露 ``store_ref``/``sku``/``target_price``;
- ``prepare``:服务端解析可信资源与版本、执行 PricingPolicy、构造可回放
  ``expected_state``;无法解析或违反策略时返回确定性拒绝,不伪造目标;
- ``execute_prepared``:只用 prepare 结果调用 Gateway,``operation_id`` 由执行
  边界注入并从其派生幂等键;APPLIED 才有观测 payload。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus, StorePlatform
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.policy.models import PricePolicyConfig
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    PriceSnapshot,
    PriceWriteReceipt,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.pricing.models import SetListingPriceInput
from agents.product_operations.pricing.service import (
    OPERATION_TYPE,
    SetListingPriceHandler,
)
from agents.product_operations.pricing.tools import SET_LISTING_PRICE_DEFINITION
from agents.product_operations.shared.write_contracts import WriteStatus
from runtime.access.actor import ActorContext
from runtime.execution.reconciliation import ReconcileResult
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


class _RecordingGateway:
    """记录写命令并返回固定回执的测试 Gateway(PricingGateway 协议)。"""

    def __init__(self, receipt: PriceWriteReceipt | None = None) -> None:
        self.command: SetListingPriceCommand | None = None
        self._receipt = receipt

    async def get_price(self, query: PriceQuery) -> PriceSnapshot:  # pragma: no cover
        raise NotImplementedError

    async def set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        self.command = command
        assert self._receipt is not None
        return self._receipt

    async def reconcile_price(  # pragma: no cover
        self, command: ReconcilePriceCommand
    ) -> ReconcileResult:
        raise NotImplementedError


@dataclass(frozen=True)
class _Env:
    workspace_id: uuid.UUID
    store: Store
    listing: Listing
    handler: SetListingPriceHandler
    gateway: _RecordingGateway
    actor: ActorContext


_POLICY_CONFIG = PricePolicyConfig(
    policy_version="price-policy-v1",
    currency="CNY",
    min_allowed_price=Decimal("50.00"),
    max_price_change=Decimal("100.00"),
)


def _actor(workspace_id: uuid.UUID, store_id: uuid.UUID) -> ActorContext:
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=frozenset({"product.read", "product.price.write"}),
        resource_scopes={
            "store": ResourceScope(mode="SELECTED", resource_ids={str(store_id)}),
        },
        request_id="req-set-price",
    )


async def _seed(session: AsyncSession) -> tuple[Workspace, Store, Listing]:
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    store = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"taobao-{uuid.uuid4()}",
        name="淘宝旗舰店",
        aliases=["旗舰店"],
    )
    product = Product(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        sku="SKU-001",
        title="轻量保温杯 500ml",
    )
    session.add(workspace)
    await session.flush()
    session.add_all((store, product))
    await session.flush()
    listing = Listing(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        store_id=store.id,
        product_id=product.id,
        external_product_ref="item-1",
        currency="CNY",
        current_price=Decimal("128.00"),
        cost_price=Decimal("88.00"),
        status=ListingStatus.PUBLISHED,
    )
    session.add(listing)
    await session.flush()
    await session.refresh(listing)
    return workspace, store, listing


_CLEANUP = (
    "DELETE FROM product_operations.listings WHERE workspace_id = :ws",
    "DELETE FROM product_operations.products WHERE workspace_id = :ws",
    "DELETE FROM product_operations.stores WHERE workspace_id = :ws",
    "DELETE FROM workspaces WHERE id = :ws",
)


@pytest.fixture()
async def env(db_session: AsyncSession) -> AsyncIterator[_Env]:
    """Handler 的 ``prepare`` 会提交读取事务,因此种子行显式提交并在用例后清理。"""
    workspace, store, listing = await _seed(db_session)
    await db_session.commit()
    actor = _actor(workspace.id, store.id)
    gateway = _RecordingGateway(
        PriceWriteReceipt(
            operation_id="unused",
            idempotency_key="unused",
            status=WriteStatus.APPLIED,
            resource_version=listing.version + 1,
            external_ref="item-1",
            changed=True,
        )
    )
    handler = SetListingPriceHandler(
        actor=actor,
        session=db_session,
        gateway=gateway,
        policy_config=_POLICY_CONFIG,
    )
    try:
        yield _Env(
            workspace_id=workspace.id,
            store=store,
            listing=listing,
            handler=handler,
            gateway=gateway,
            actor=actor,
        )
    finally:
        for statement in _CLEANUP:
            await db_session.execute(sa.text(statement), {"ws": workspace.id})
        await db_session.commit()


def test_set_listing_price_has_only_business_inputs() -> None:
    assert set(SET_LISTING_PRICE_DEFINITION.input_json_schema["properties"]) == {
        "store_ref",
        "sku",
        "target_price",
    }
    assert set(SetListingPriceInput.model_fields) == {"store_ref", "sku", "target_price"}
    # 输入模型本身不接受内部标识字段。
    assert "store_id" not in SetListingPriceInput.model_fields


async def test_prepare_builds_trusted_command_and_expected_state(env: _Env) -> None:
    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": Decimal("100.00"),
        }
    )
    command = prepared.command
    assert isinstance(command, object)
    assert prepared.expected_state is not None
    expected_state: Mapping[str, object] = prepared.expected_state
    assert expected_state["operation_type"] == OPERATION_TYPE
    assert expected_state["listing_id"] == str(env.listing.id)
    assert expected_state["currency"] == "CNY"
    assert expected_state["expected_version"] == env.listing.version
    assert expected_state["target_price"] == "100.00"


async def test_prepare_rejects_unknown_store_ref(env: _Env) -> None:
    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "不存在的店",
            "sku": "SKU-001",
            "target_price": Decimal("100.00"),
        }
    )
    assert prepared.expected_state is None
    assert prepared.command is None
    assert prepared.rejection is not None
    # 无匹配店铺是领域 NOT_FOUND,不是"Listing 不存在"。
    assert prepared.rejection.error_code == "NOT_FOUND"
    assert "rule_code" not in prepared.rejection.context


async def test_prepare_rejects_ambiguous_store_ref(env: _Env, db_session: AsyncSession) -> None:
    """同名店铺匹配到多个:AMBIGUOUS_STORE_REFERENCE,需用户补充输入。"""
    db_session.add(
        Store(
            id=uuid.uuid4(),
            workspace_id=env.workspace_id,
            platform=StorePlatform.TAOBAO,
            external_store_ref=f"taobao-{uuid.uuid4()}",
            name="淘宝旗舰店",
            aliases=[],
        )
    )
    await db_session.commit()

    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": Decimal("100.00"),
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "AMBIGUOUS_STORE_REFERENCE"


async def test_prepare_rejects_out_of_scope_store(
    env: _Env, db_session: AsyncSession
) -> None:
    """命中店铺但不在授权范围:RESOURCE_SCOPE_DENIED,不伪装成资源不存在。"""
    restricted = _actor(env.workspace_id, uuid.uuid4())
    handler = SetListingPriceHandler(
        actor=restricted,
        session=db_session,
        gateway=_RecordingGateway(),
        policy_config=_POLICY_CONFIG,
    )
    prepared = await handler.prepare(
        normalized_arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": Decimal("100.00"),
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "RESOURCE_SCOPE_DENIED"


async def test_prepare_rejects_missing_listing(env: _Env) -> None:
    """店铺存在但无该 SKU 的 Listing:LISTING_NOT_FOUND。"""
    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-404",
            "target_price": Decimal("100.00"),
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "LISTING_NOT_FOUND"


async def test_prepare_rejects_price_below_floor(env: _Env) -> None:
    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": Decimal("10.00"),
        }
    )
    assert prepared.rejection is not None
    # 规则拒绝归 VALIDATION_FAILED,具体规则保留在 rule_code。
    assert prepared.rejection.error_code == "VALIDATION_FAILED"
    assert prepared.rejection.context["rule_code"] == "PRICE_BELOW_FLOOR"


async def test_prepare_rejects_unchanged_price(env: _Env) -> None:
    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "淘宝旗舰店",
            "sku": "SKU-001",
            "target_price": Decimal("128.00"),
        }
    )
    assert prepared.rejection is not None
    assert prepared.rejection.error_code == "VALIDATION_FAILED"
    assert prepared.rejection.context["rule_code"] == "PRICE_UNCHANGED"


async def test_execute_prepared_uses_operation_id_and_injected_idempotency(
    env: _Env,
) -> None:
    prepared = await env.handler.prepare(
        normalized_arguments={
            "store_ref": "旗舰店",
            "sku": "SKU-001",
            "target_price": Decimal("100.00"),
        }
    )
    operation_id = uuid.uuid4()
    injected_key = f"set_listing_price:{operation_id}"
    receipt = await env.handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=operation_id,
        idempotency_key=injected_key,
        prepared=prepared,
    )
    command = env.gateway.command
    assert command is not None
    assert command.operation_id == str(operation_id)
    # 幂等键来自 Runtime 注入,不由命令自行派生、也不来自模型输入。
    assert command.idempotency_key == injected_key
    assert command.currency == "CNY"
    assert command.expected_version == env.listing.version
    assert command.target_price == Decimal("100.00")
    assert receipt.status == "APPLIED"
    assert receipt.payload is not None
    assert receipt.payload["target_price"] == "100.00"


async def test_execute_prepared_unknown_receipt_has_no_payload(db_session: AsyncSession) -> None:
    workspace, store, _listing = await _seed(db_session)
    gateway = _RecordingGateway(
        PriceWriteReceipt(
            operation_id="op-unknown",
            idempotency_key="op-unknown",
            status=WriteStatus.UNKNOWN,
            reason_code="PLATFORM_OUTCOME_UNKNOWN",
        )
    )
    handler = SetListingPriceHandler(
        actor=_actor(workspace.id, store.id),
        session=db_session,
        gateway=gateway,
        policy_config=_POLICY_CONFIG,
    )
    try:
        prepared = await handler.prepare(
            normalized_arguments={
                "store_ref": "淘宝旗舰店",
                "sku": "SKU-001",
                "target_price": Decimal("100.00"),
            }
        )
        receipt = await handler.execute_prepared(
            tool_call_id=uuid.uuid4(),
            operation_id=uuid.uuid4(),
            idempotency_key="ignored",
            prepared=prepared,
        )
        assert receipt.status == "UNKNOWN"
        assert receipt.payload is None
    finally:
        for statement in _CLEANUP:
            await db_session.execute(sa.text(statement), {"ws": workspace.id})
        await db_session.commit()
