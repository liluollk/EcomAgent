"""Listing 查询能力测试(P1 Task 5;设计 §4.5、§4.1)。

覆盖:
- ``get_listing`` 输入 Schema 只暴露 ``store_ref`` / ``sku``,不含内部 ID;
- 快照返回规范发布状态 ``PUBLISHED`` / ``UNPUBLISHED``;未发布 Listing 仍可读;
- 店铺引用按名称/别名解析;未知/歧义/越权引用各自确定性失败;
- 缺少 ``product.read`` 权限拒绝;禁用商品以 ``issues`` 表达。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import (
    ListingStatus,
    ProductStatus,
    StorePlatform,
)
from agents.product_operations.listing.models import GetListingInput
from agents.product_operations.listing.service import PRODUCT_DISABLED_ISSUE, ListingService
from agents.product_operations.listing.tools import GET_LISTING_DEFINITION
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.shared.errors import (
    AMBIGUOUS_STORE_REFERENCE,
    NOT_FOUND,
    ProductOperationError,
)
from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext
from runtime.access.authorization import PERMISSION_DENIED, RESOURCE_SCOPE_DENIED
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole
from runtime.persistence.models.identity import Workspace


@dataclass(frozen=True)
class _Seed:
    workspace: Workspace
    alpha: Store
    beta: Store
    gamma: Store


async def _seed(session: AsyncSession) -> _Seed:
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
    # alpha 与 beta 共享别名 "旗舰店",用于歧义引用;gamma 仅按名称引用。
    alpha = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"alpha-{uuid.uuid4()}",
        name="淘宝一号店",
        aliases=["旗舰店"],
    )
    beta = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.DOUYIN,
        external_store_ref=f"beta-{uuid.uuid4()}",
        name="豆音二号店",
        aliases=["旗舰店"],
    )
    gamma = Store(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        platform=StorePlatform.TAOBAO,
        external_store_ref=f"gamma-{uuid.uuid4()}",
        name="淘宝三号店",
        aliases=[],
    )
    enabled = Product(
        id=uuid.uuid4(), workspace_id=workspace.id, sku="SKU-001", title="轻量保温杯"
    )
    disabled = Product(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        sku="SKU-002",
        title="停产马克杯",
        status=ProductStatus.DISABLED,
    )
    session.add(workspace)
    await session.flush()
    session.add_all((alpha, beta, gamma, enabled, disabled))
    await session.flush()
    session.add_all(
        (
            Listing(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                store_id=alpha.id,
                product_id=enabled.id,
                external_product_ref="item-1",
                currency="CNY",
                current_price=Decimal("128.00"),
                cost_price=Decimal("88.00"),
            ),
            Listing(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                store_id=alpha.id,
                product_id=disabled.id,
                external_product_ref="item-2",
                currency="CNY",
                current_price=Decimal("45.50"),
                cost_price=Decimal("30.00"),
            ),
        )
    )
    await session.flush()
    return _Seed(workspace=workspace, alpha=alpha, beta=beta, gamma=gamma)


def _actor(
    *,
    workspace_id: uuid.UUID,
    store_ids: frozenset[str],
    permissions: frozenset[str] = frozenset({"product.read"}),
) -> ActorContext:
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=workspace_id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=permissions,
        resource_scopes={
            "store": ResourceScope(mode="SELECTED", resource_ids=store_ids),
        },
        request_id="req-listing",
    )


@dataclass(frozen=True)
class _Env:
    seed: _Seed
    service: ListingService
    operator: ActorContext


@pytest.fixture()
async def env(db_session: AsyncSession) -> _Env:
    seed = await _seed(db_session)
    return _Env(
        seed=seed,
        service=ListingService(db_session),
        operator=_actor(
            workspace_id=seed.workspace.id, store_ids=frozenset({str(seed.alpha.id)})
        ),
    )


def test_get_listing_schema_exposes_refs_not_internal_ids() -> None:
    assert set(GET_LISTING_DEFINITION.input_json_schema["properties"]) == {
        "store_ref",
        "sku",
    }
    assert set(GetListingInput.model_fields) == {"store_ref", "sku"}


async def test_get_listing_returns_snapshot_with_publication_state(env: _Env) -> None:
    snapshot = await env.service.get_listing(
        actor=env.operator, store_ref="淘宝一号店", sku="SKU-001"
    )
    assert snapshot.store_ref == "淘宝一号店"
    assert snapshot.sku == "SKU-001"
    assert snapshot.title == "轻量保温杯"
    assert snapshot.current_price == Decimal("128.00")
    assert snapshot.currency == "CNY"
    assert snapshot.publication_state == "PUBLISHED"
    assert snapshot.listing_version == 1
    assert snapshot.issues == ()
    # 内部标识不作为模型可见字段出现。
    assert str(env.seed.alpha.id) not in snapshot.model_dump_json()


async def test_get_listing_reads_unpublished_listing(
    env: _Env, db_session: AsyncSession
) -> None:
    row = await db_session.scalar(
        select(Listing)
        .join(Product, Listing.product_id == Product.id)
        .where(
            Listing.workspace_id == env.seed.workspace.id,
            Listing.store_id == env.seed.alpha.id,
            Product.sku == "SKU-001",
        )
    )
    assert row is not None
    row.status = ListingStatus.UNPUBLISHED
    await db_session.flush()

    snapshot = await env.service.get_listing(
        actor=env.operator, store_ref="淘宝一号店", sku="SKU-001"
    )
    assert snapshot.publication_state == "UNPUBLISHED"


async def test_get_listing_reports_disabled_product_issue(env: _Env) -> None:
    snapshot = await env.service.get_listing(
        actor=env.operator, store_ref="淘宝一号店", sku="SKU-002"
    )
    assert snapshot.issues == (PRODUCT_DISABLED_ISSUE,)


async def test_get_listing_unknown_sku_is_not_found(env: _Env) -> None:
    with pytest.raises(ProductOperationError) as error:
        await env.service.get_listing(
            actor=env.operator, store_ref="淘宝一号店", sku="SKU-404"
        )
    assert error.value.code == NOT_FOUND


async def test_get_listing_unknown_store_ref_is_not_found(env: _Env) -> None:
    with pytest.raises(ProductOperationError) as error:
        await env.service.get_listing(
            actor=env.operator, store_ref="不存在的店铺", sku="SKU-001"
        )
    assert error.value.code == NOT_FOUND


async def test_get_listing_ambiguous_store_ref_is_rejected(env: _Env) -> None:
    with pytest.raises(ProductOperationError) as error:
        await env.service.get_listing(
            actor=env.operator, store_ref="旗舰店", sku="SKU-001"
        )
    assert error.value.code == AMBIGUOUS_STORE_REFERENCE


async def test_get_listing_out_of_scope_store_ref_is_denied(env: _Env) -> None:
    with pytest.raises(ToolAccessError) as error:
        await env.service.get_listing(
            actor=env.operator, store_ref="淘宝三号店", sku="SKU-001"
        )
    assert error.value.reason_code == RESOURCE_SCOPE_DENIED


async def test_get_listing_requires_read_permission(env: _Env) -> None:
    actor = _actor(
        workspace_id=env.seed.workspace.id,
        store_ids=frozenset({str(env.seed.alpha.id)}),
        permissions=frozenset(),
    )
    with pytest.raises(ToolAccessError) as error:
        await env.service.get_listing(
            actor=actor, store_ref="淘宝一号店", sku="SKU-001"
        )
    assert error.value.reason_code == PERMISSION_DENIED
