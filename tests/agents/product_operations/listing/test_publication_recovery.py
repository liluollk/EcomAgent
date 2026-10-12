"""刊登发布状态回查、幂等与恢复测试(P1 Task 8;设计 §5.3、§6)。

覆盖:
- ``build_reconcile_listing_command`` 只从 ``Operation.expected_state`` 构造命令,
  缺字段 / 错 ``operation_type`` / 非法目标状态一律 fail closed;
- :class:`ListingReconciler` 委托注入的 Gateway,目标状态只来自持久化预期;
- Mock Adapter 按 ``operation_id`` 幂等去重、用 CAS 更新 ``listings.status``;
  同目标是幂等 no-op,回查同时校验操作记录、资源身份与目标状态,任一不一致都
  fail closed,绝不猜测 ``APPLIED``;
- 同一 ``operation_id`` 被复用到另一个 Listing、另一个目标状态或另一个原因时
  确定失败,不回放旧成功回执。
"""

from __future__ import annotations

import dataclasses
import uuid
from decimal import Decimal

import pytest
from publication_support import PublicationEnv

from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.protocol import (
    REASON_IDEMPOTENCY_CONFLICT,
    REASON_PLATFORM_FAILURE,
    REASON_TIMEOUT_BEFORE_WRITE,
)
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingPublicationSnapshot,
    ListingQuery,
    ListingUnavailable,
    ReconcileListingCommand,
)
from agents.product_operations.listing.models import UnpublishReason
from agents.product_operations.listing.reconciliation import (
    ListingReconciler,
    build_reconcile_listing_command,
)
from agents.product_operations.listing.service import (
    OPERATION_TYPE,
    PreparedChangePublication,
)
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.shared.errors import VALIDATION_FAILED, ProductOperationError
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.execution.reconciliation import ReconcileResult


def _prepared(
    *, target_state: ListingStatus = ListingStatus.PUBLISHED, reason: str | None = None
) -> PreparedChangePublication:
    return PreparedChangePublication(
        workspace_id=uuid.uuid4(),
        store_ref="旗舰店",
        store_id=uuid.uuid4(),
        listing_id=uuid.uuid4(),
        external_store_ref="shop-1",
        external_product_ref="item-1",
        sku="SKU-1",
        target_state=target_state,
        reason=reason,
        expected_version=1,
        policy_version="listing-policy-v1",
    )


class _FixedGateway:
    """返回固定回查结论的测试 Gateway。"""

    def __init__(self, result: ReconcileResult) -> None:
        self.command: ReconcileListingCommand | None = None
        self._result = result

    async def get_listing(  # pragma: no cover
        self, query: ListingQuery
    ) -> ListingPublicationSnapshot:
        raise NotImplementedError

    async def publish_listing(  # pragma: no cover
        self, command: ChangePublicationCommand
    ) -> WriteReceipt:
        raise NotImplementedError

    async def unpublish_listing(  # pragma: no cover
        self, command: ChangePublicationCommand
    ) -> WriteReceipt:
        raise NotImplementedError

    async def reconcile_listing(self, command: ReconcileListingCommand) -> ReconcileResult:
        self.command = command
        return self._result


def test_build_reconcile_listing_command_uses_expected_state() -> None:
    prepared = _prepared(reason=UnpublishReason.MANUAL.value)
    expected_state = prepared.expected_state()
    command = build_reconcile_listing_command(
        operation_id="op-1",
        expected_state=expected_state,
        workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
    )
    assert command.operation_id == "op-1"
    assert command.listing_id == uuid.UUID(str(expected_state["listing_id"]))
    assert command.expected_state is ListingStatus.PUBLISHED
    assert command.external_product_ref == "item-1"


def test_build_reconcile_listing_command_rejects_missing_fields() -> None:
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_listing_command(
            operation_id="op-1",
            expected_state={"operation_type": OPERATION_TYPE},
            workspace_id=uuid.uuid4(),
        )
    assert exc.value.code == VALIDATION_FAILED


def test_build_reconcile_listing_command_rejects_foreign_operation_type() -> None:
    expected_state = _prepared().expected_state()
    expected_state["operation_type"] = "SET_LISTING_PRICE"
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_listing_command(
            operation_id="op-1", expected_state=expected_state, workspace_id=uuid.uuid4()
        )
    assert exc.value.code == VALIDATION_FAILED


def test_build_reconcile_listing_command_rejects_unknown_target_state() -> None:
    """目标状态必须是受控枚举取值;脏数据不得变成"默认回查"。"""
    expected_state = _prepared().expected_state()
    expected_state["target_state"] = "ARCHIVED"
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_listing_command(
            operation_id="op-1",
            expected_state=expected_state,
            workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
        )
    assert exc.value.code == VALIDATION_FAILED


def test_build_reconcile_listing_command_rejects_workspace_mismatch() -> None:
    """持久化 Workspace 与当前 Workspace 不一致 → fail closed,不得改用当前值回查。"""
    expected_state = _prepared().expected_state()
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_listing_command(
            operation_id="op-1",
            expected_state=expected_state,
            workspace_id=uuid.uuid4(),
        )
    assert exc.value.code == VALIDATION_FAILED


async def test_listing_reconciler_delegates_target_state() -> None:
    gateway = _FixedGateway(ReconcileResult.NOT_APPLIED)
    expected_state = _prepared(target_state=ListingStatus.UNPUBLISHED).expected_state()
    reconciler = ListingReconciler(
        gateway,
        workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
        operation_id="op-1",
    )

    result = await reconciler.reconcile(expected_state)

    assert result is ReconcileResult.NOT_APPLIED
    assert gateway.command is not None
    assert gateway.command.expected_state is ListingStatus.UNPUBLISHED
    assert gateway.command.operation_id == "op-1"


# ---------------------------------------------------------------------------
# Mock Adapter:get / publish / unpublish / reconcile
# ---------------------------------------------------------------------------


def _query(env: PublicationEnv) -> ListingQuery:
    return ListingQuery(
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        listing_id=env.listing.id,
        external_store_ref=env.store.external_store_ref,
        external_product_ref=env.listing.external_product_ref,
    )


def _command(
    env: PublicationEnv,
    *,
    operation_id: str,
    expected_version: int | None = None,
    reason: str | None = None,
) -> ChangePublicationCommand:
    return ChangePublicationCommand(
        operation_id=operation_id,
        idempotency_key=f"change_listing_publication:{operation_id}",
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        listing_id=env.listing.id,
        external_store_ref=env.store.external_store_ref,
        external_product_ref=env.listing.external_product_ref,
        sku=env.product.sku,
        reason=reason,
        expected_version=(
            env.listing.version if expected_version is None else expected_version
        ),
    )


def _reconcile(
    env: PublicationEnv,
    *,
    operation_id: str,
    expected_state: ListingStatus = ListingStatus.PUBLISHED,
) -> ReconcileListingCommand:
    return ReconcileListingCommand(
        operation_id=operation_id,
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        listing_id=env.listing.id,
        external_store_ref=env.store.external_store_ref,
        external_product_ref=env.listing.external_product_ref,
        expected_state=expected_state,
    )


async def _add_twin_listing(
    env: PublicationEnv, *, status: ListingStatus
) -> Listing:
    """同 Workspace/店铺下再建一个 Listing(独立商品),状态与目标相同。"""
    twin_product = Product(
        id=uuid.uuid4(),
        workspace_id=env.workspace_id,
        sku=f"SKU-{uuid.uuid4()}",
        title="twin-product",
    )
    env.session.add(twin_product)
    await env.session.flush()
    twin_listing = Listing(
        id=uuid.uuid4(),
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        product_id=twin_product.id,
        external_product_ref=f"item-{uuid.uuid4()}",
        currency="CNY",
        current_price=Decimal("99.00"),
        cost_price=Decimal("88.00"),
        status=status,
    )
    env.session.add(twin_listing)
    await env.session.flush()
    return twin_listing


async def test_mock_get_listing_returns_authoritative_snapshot(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    snapshot = await adapter.get_listing(_query(publication_env))
    assert snapshot.publication_state is ListingStatus.PUBLISHED
    assert snapshot.listing_version == publication_env.listing.version


async def test_mock_get_listing_raises_when_missing(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    missing = dataclasses.replace(_query(publication_env), listing_id=uuid.uuid4())
    with pytest.raises(ListingUnavailable):
        await adapter.get_listing(missing)


async def test_mock_unpublish_observes_version_bump(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    base_version = publication_env.listing.version

    receipt = await adapter.unpublish_listing(
        _command(publication_env, operation_id="op-1")
    )

    assert receipt.status is WriteStatus.APPLIED
    assert receipt.changed is True
    assert receipt.resource_version == base_version + 1
    listing = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, publication_env.listing.id
    )
    assert listing is not None and listing.status is ListingStatus.UNPUBLISHED


async def test_mock_publish_same_state_is_noop_without_version_bump(
    publication_env: PublicationEnv,
) -> None:
    """目标与当前相同:APPLIED + changed=False,版本不变、零逻辑写。"""
    adapter = MockCommerceAdapter(publication_env.session)
    base_version = publication_env.listing.version

    receipt = await adapter.publish_listing(
        _command(publication_env, operation_id="op-1")
    )

    assert receipt.status is WriteStatus.APPLIED
    assert receipt.changed is False
    assert receipt.resource_version == base_version
    assert await adapter.count_logical_writes("op-1") == 0
    listing = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, publication_env.listing.id
    )
    assert listing is not None and listing.version == base_version


async def test_mock_reconcile_recovers_after_response_loss(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    adapter.faults.inject("response_lost_after_commit")

    receipt = await adapter.unpublish_listing(
        _command(publication_env, operation_id="op-1")
    )
    assert receipt.status is WriteStatus.UNKNOWN
    assert receipt.changed is None and receipt.resource_version is None

    adapter.faults.clear()
    result = await adapter.reconcile_listing(
        _reconcile(
            publication_env,
            operation_id="op-1",
            expected_state=ListingStatus.UNPUBLISHED,
        )
    )
    assert result is ReconcileResult.APPLIED


async def test_mock_reconcile_not_applied_before_write(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    result = await adapter.reconcile_listing(
        _reconcile(publication_env, operation_id="op-absent")
    )
    assert result is ReconcileResult.NOT_APPLIED


async def test_mock_timeout_before_write_leaves_state_untouched(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    adapter.faults.inject("timeout_before_write")
    base_version = publication_env.listing.version

    receipt = await adapter.unpublish_listing(
        _command(publication_env, operation_id="op-1")
    )

    assert receipt.status is WriteStatus.UNKNOWN
    assert receipt.reason_code == REASON_TIMEOUT_BEFORE_WRITE
    assert await adapter.count_logical_writes("op-1") == 0
    listing = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, publication_env.listing.id
    )
    assert listing is not None
    assert listing.version == base_version
    assert listing.status is ListingStatus.PUBLISHED


async def test_mock_permanent_failure_is_definite_and_never_unknown(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    adapter.faults.inject("permanent_failure")

    receipt = await adapter.unpublish_listing(
        _command(publication_env, operation_id="op-1")
    )

    assert receipt.status is WriteStatus.FAILED
    assert receipt.reason_code == REASON_PLATFORM_FAILURE
    assert await adapter.count_logical_writes("op-1") == 0


async def test_mock_unknown_listing_fails_without_write(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    missing = dataclasses.replace(
        _command(publication_env, operation_id="op-1"), workspace_id=uuid.uuid4()
    )

    receipt = await adapter.unpublish_listing(missing)

    assert receipt.status is WriteStatus.FAILED
    assert receipt.reason_code == "LISTING_NOT_FOUND"
    assert await adapter.count_logical_writes("op-1") == 0


async def test_mock_cas_version_conflict_is_not_unknown(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    stale = _command(
        publication_env,
        operation_id="op-stale",
        expected_version=publication_env.listing.version + 1,
    )

    receipt = await adapter.unpublish_listing(stale)

    assert receipt.status is WriteStatus.FAILED
    assert receipt.reason_code == "VERSION_CONFLICT"
    assert await adapter.count_logical_writes("op-stale") == 0
    listing = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, publication_env.listing.id
    )
    assert listing is not None
    assert listing.status is ListingStatus.PUBLISHED
    assert listing.version == publication_env.listing.version


# ---------------------------------------------------------------------------
# 回查绑定:错 Listing / 错目标状态一律 fail closed
# ---------------------------------------------------------------------------


async def test_reconcile_rejects_other_listing_with_same_state(
    publication_env: PublicationEnv,
) -> None:
    """错 Listing 但目标状态恰好相同 → 不得判定 APPLIED(回查绑定资源身份)。"""
    adapter = MockCommerceAdapter(publication_env.session)
    applied = await adapter.publish_listing(_command(publication_env, operation_id="op-1"))
    assert applied.status is WriteStatus.APPLIED

    twin = await _add_twin_listing(publication_env, status=ListingStatus.PUBLISHED)

    # 自证陷阱有效:另一个 Listing 的状态确实等于目标状态,只看状态的实现会被骗过。
    twin_row = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, twin.id
    )
    assert twin_row is not None and twin_row.status is ListingStatus.PUBLISHED

    misrouted = dataclasses.replace(
        _reconcile(publication_env, operation_id="op-1"),
        listing_id=twin.id,
        external_product_ref=twin.external_product_ref,
    )
    assert await adapter.reconcile_listing(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reconcile_rejects_target_state_that_was_not_recorded(
    publication_env: PublicationEnv,
) -> None:
    """目标状态与记录不一致 → 不得因"当前恰好是该状态"判定 APPLIED。

    先真实迁移到 PUBLISHED 并记录目标 PUBLISHED,再由别的操作改回 UNPUBLISHED;
    此时权威状态等于回查命令的目标状态,只看状态的实现会误判 APPLIED。
    """
    publication_env.listing.status = ListingStatus.UNPUBLISHED
    await publication_env.session.commit()

    adapter = MockCommerceAdapter(publication_env.session)
    applied = await adapter.publish_listing(_command(publication_env, operation_id="op-1"))
    assert applied.status is WriteStatus.APPLIED and applied.changed is True

    publication_env.listing.status = ListingStatus.UNPUBLISHED
    await publication_env.session.commit()

    misrouted = _reconcile(
        publication_env, operation_id="op-1", expected_state=ListingStatus.UNPUBLISHED
    )
    assert await adapter.reconcile_listing(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reconcile_rejects_other_workspace(
    publication_env: PublicationEnv,
) -> None:
    adapter = MockCommerceAdapter(publication_env.session)
    await adapter.unpublish_listing(_command(publication_env, operation_id="op-1"))

    misrouted = dataclasses.replace(
        _reconcile(
            publication_env,
            operation_id="op-1",
            expected_state=ListingStatus.UNPUBLISHED,
        ),
        workspace_id=uuid.uuid4(),
    )
    assert await adapter.reconcile_listing(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reconcile_applies_only_for_the_recorded_target(
    publication_env: PublicationEnv,
) -> None:
    """对照面:同一 operation_id + 记录的资源身份与目标状态 → 正常返回 APPLIED。"""
    adapter = MockCommerceAdapter(publication_env.session)
    await adapter.unpublish_listing(_command(publication_env, operation_id="op-1"))

    result = await adapter.reconcile_listing(
        _reconcile(
            publication_env,
            operation_id="op-1",
            expected_state=ListingStatus.UNPUBLISHED,
        )
    )
    assert result is ReconcileResult.APPLIED


async def test_reconcile_of_noop_returns_applied(
    publication_env: PublicationEnv,
) -> None:
    """幂等 no-op 也要能被回查确认(记录 committed,权威状态等于目标)。"""
    adapter = MockCommerceAdapter(publication_env.session)
    await adapter.publish_listing(_command(publication_env, operation_id="op-1"))

    result = await adapter.reconcile_listing(
        _reconcile(publication_env, operation_id="op-1")
    )
    assert result is ReconcileResult.APPLIED


# ---------------------------------------------------------------------------
# 写路径幂等:同一 operation_id 换资源 / 换目标 / 换原因 → 确定失败
# ---------------------------------------------------------------------------


async def test_reused_operation_id_on_another_listing_fails_closed(
    publication_env: PublicationEnv,
) -> None:
    """同一 operation_id 作用到另一个 Listing → 确定失败,不回放旧成功回执。"""
    adapter = MockCommerceAdapter(publication_env.session)
    first = await adapter.unpublish_listing(_command(publication_env, operation_id="op-1"))
    assert first.status is WriteStatus.APPLIED

    twin = await _add_twin_listing(publication_env, status=ListingStatus.PUBLISHED)
    conflicting = await adapter.unpublish_listing(
        dataclasses.replace(
            _command(publication_env, operation_id="op-1"),
            listing_id=twin.id,
            external_product_ref=twin.external_product_ref,
        )
    )

    assert conflicting.status is WriteStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await adapter.count_logical_writes("op-1") == 1
    twin_row = await ProductRepository(publication_env.session).get_listing(
        publication_env.workspace_id, twin.id
    )
    assert twin_row is not None and twin_row.status is ListingStatus.PUBLISHED


@pytest.mark.parametrize(
    ("second_call", "reason"),
    [
        ("publish", None),
        ("unpublish", UnpublishReason.COMPLIANCE.value),
    ],
)
async def test_reused_operation_id_with_different_intent_fails_closed(
    publication_env: PublicationEnv, second_call: str, reason: str | None
) -> None:
    """同一 operation_id、同一 Listing,但换了目标状态或下架原因 → 确定失败。

    只比较资源身份的旧实现会把"另一个业务动作"回放成本次成功;写路径必须比对
    业务意图指纹(目标发布状态与原因都在其中)。
    """
    adapter = MockCommerceAdapter(publication_env.session)
    first = await adapter.unpublish_listing(
        _command(publication_env, operation_id="op-1", reason=UnpublishReason.MANUAL.value)
    )
    assert first.status is WriteStatus.APPLIED

    command = _command(publication_env, operation_id="op-1", reason=reason)
    conflicting = (
        await adapter.publish_listing(command)
        if second_call == "publish"
        else await adapter.unpublish_listing(command)
    )

    assert conflicting.status is WriteStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await adapter.count_logical_writes("op-1") == 1


async def test_duplicate_submit_with_reread_version_still_replays(
    publication_env: PublicationEnv,
) -> None:
    """对照面:意图相同、仅 expected_version 因重读变化的重复提交仍回放旧回执。"""
    adapter = MockCommerceAdapter(publication_env.session)
    first = await adapter.unpublish_listing(
        _command(publication_env, operation_id="op-1")
    )
    assert first.status is WriteStatus.APPLIED

    replay = await adapter.unpublish_listing(
        _command(
            publication_env,
            operation_id="op-1",
            expected_version=publication_env.listing.version + 1,
        )
    )

    assert replay == first
    assert await adapter.count_logical_writes("op-1") == 1
