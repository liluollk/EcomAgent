"""库存回查、幂等与恢复测试(P1 Task 7;设计 §5.3、§6)。

覆盖:
- ``build_reconcile_inventory_command`` 只从 ``Operation.expected_state`` 构造命令,
  缺字段/错 ``operation_type`` fail closed;
- :class:`InventoryReconciler` 委托注入的 Gateway,目标数量只来自持久化预期;
- Mock Adapter 按 ``operation_id`` 幂等去重、用 CAS 更新数量;回查同时校验操作
  记录与预期调整后数量,权威数量与预期不一致(疑似并发变更)时返回 ``UNKNOWN``,
  绝不猜测 ``APPLIED``。
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from _support import InventoryEnv, add_active_location

from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.protocol import REASON_IDEMPOTENCY_CONFLICT
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    InventoryLevelSnapshot,
    InventoryQuery,
    InventoryUnavailable,
    InventoryWriteReceipt,
    ReconcileInventoryCommand,
)
from agents.product_operations.inventory.reconciliation import (
    InventoryReconciler,
    build_reconcile_inventory_command,
)
from agents.product_operations.inventory.service import OPERATION_TYPE, PreparedAdjustInventory
from agents.product_operations.persistence.models import InventoryLevel, Product
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.shared.errors import VALIDATION_FAILED, ProductOperationError
from agents.product_operations.shared.write_contracts import WriteStatus
from runtime.execution.reconciliation import ReconcileResult


def _prepared(*, delta: int = 5, quantity_before: int = 20) -> PreparedAdjustInventory:
    return PreparedAdjustInventory(
        workspace_id=uuid.uuid4(),
        store_ref="旗舰店",
        store_id=uuid.uuid4(),
        location_ref="默认仓",
        location_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        external_store_ref="shop-1",
        external_location_ref="loc-1",
        external_product_ref="item-1",
        sku="SKU-1",
        delta=delta,
        reason="RECEIPT",
        quantity_before=quantity_before,
        expected_version=1,
        policy_version="inventory-policy-v1",
    )


class _FixedGateway:
    """返回固定回查结论的测试 Gateway。"""

    def __init__(self, result: ReconcileResult) -> None:
        self.command: ReconcileInventoryCommand | None = None
        self._result = result

    async def get_inventory(  # pragma: no cover
        self, query: InventoryQuery
    ) -> InventoryLevelSnapshot:
        raise NotImplementedError

    async def adjust_inventory(  # pragma: no cover
        self, command: AdjustInventoryCommand
    ) -> InventoryWriteReceipt:
        raise NotImplementedError

    async def reconcile_inventory(
        self, command: ReconcileInventoryCommand
    ) -> ReconcileResult:
        self.command = command
        return self._result


def test_build_reconcile_inventory_command_uses_expected_state() -> None:
    expected_state = _prepared().expected_state()
    command = build_reconcile_inventory_command(
        operation_id="op-1",
        expected_state=expected_state,
        workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
    )
    assert command.operation_id == "op-1"
    assert command.location_id == uuid.UUID(str(expected_state["location_id"]))
    assert command.product_id == uuid.UUID(str(expected_state["product_id"]))
    assert command.quantity_before == 20
    assert command.expected_quantity_after == 25
    assert command.external_location_ref == "loc-1"


def test_build_reconcile_inventory_command_rejects_missing_fields() -> None:
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_inventory_command(
            operation_id="op-1",
            expected_state={"operation_type": OPERATION_TYPE},
            workspace_id=uuid.uuid4(),
        )
    assert exc.value.code == VALIDATION_FAILED


def test_build_reconcile_inventory_command_rejects_foreign_operation_type() -> None:
    expected_state = _prepared().expected_state()
    expected_state["operation_type"] = "SET_LISTING_PRICE"
    with pytest.raises(ProductOperationError) as exc:
        build_reconcile_inventory_command(
            operation_id="op-1", expected_state=expected_state, workspace_id=uuid.uuid4()
        )
    assert exc.value.code == VALIDATION_FAILED


async def test_inventory_reconciler_delegates_expected_quantity() -> None:
    gateway = _FixedGateway(ReconcileResult.NOT_APPLIED)
    expected_state = _prepared().expected_state()
    reconciler = InventoryReconciler(
        gateway,
        workspace_id=uuid.UUID(str(expected_state["workspace_id"])),
        operation_id="op-1",
    )
    result = await reconciler.reconcile(expected_state)
    assert result is ReconcileResult.NOT_APPLIED
    assert gateway.command is not None
    assert gateway.command.expected_quantity_after == 25
    assert gateway.command.operation_id == "op-1"


# ---------------------------------------------------------------------------
# Mock Adapter:get / adjust / reconcile
# ---------------------------------------------------------------------------


def _command(
    env: InventoryEnv,
    *,
    operation_id: str,
    quantity_before: int,
    expected_version: int,
    delta: int = 5,
) -> AdjustInventoryCommand:
    return AdjustInventoryCommand(
        operation_id=operation_id,
        idempotency_key=f"adjust_inventory_quantity:{operation_id}",
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        location_id=env.location.id,
        product_id=env.product.id,
        external_store_ref=env.store.external_store_ref,
        external_location_ref=env.location.external_location_ref,
        external_product_ref=env.listing.external_product_ref,
        sku=env.product.sku,
        delta=delta,
        reason="RECEIPT",
        expected_version=expected_version,
        quantity_before=quantity_before,
    )


def _reconcile(
    env: InventoryEnv,
    *,
    operation_id: str,
    quantity_before: int,
    delta: int = 5,
) -> ReconcileInventoryCommand:
    return ReconcileInventoryCommand(
        operation_id=operation_id,
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        location_id=env.location.id,
        product_id=env.product.id,
        external_store_ref=env.store.external_store_ref,
        external_location_ref=env.location.external_location_ref,
        external_product_ref=env.listing.external_product_ref,
        quantity_before=quantity_before,
        expected_quantity_after=quantity_before + delta,
    )


def _query(env: InventoryEnv) -> InventoryQuery:
    return InventoryQuery(
        workspace_id=env.workspace_id,
        store_id=env.store.id,
        location_id=env.location.id,
        product_id=env.product.id,
        external_store_ref=env.store.external_store_ref,
        external_location_ref=env.location.external_location_ref,
        external_product_ref=env.listing.external_product_ref,
    )


async def test_mock_get_inventory_returns_authoritative_snapshot(
    inventory_env: InventoryEnv,
) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    snapshot = await adapter.get_inventory(_query(inventory_env))
    assert snapshot.available_quantity == 20
    assert snapshot.inventory_version == inventory_env.level.version


async def test_mock_get_inventory_raises_when_missing(inventory_env: InventoryEnv) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    missing = InventoryQuery(
        workspace_id=inventory_env.workspace_id,
        store_id=inventory_env.store.id,
        location_id=uuid.uuid4(),
        product_id=inventory_env.product.id,
        external_store_ref=inventory_env.store.external_store_ref,
        external_location_ref=inventory_env.location.external_location_ref,
        external_product_ref=inventory_env.listing.external_product_ref,
    )
    with pytest.raises(InventoryUnavailable):
        await adapter.get_inventory(missing)


async def test_mock_adjust_inventory_observes_before_after_and_version(
    inventory_env: InventoryEnv,
) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = inventory_env.level.available_quantity
    base_version = inventory_env.level.version
    receipt = await adapter.adjust_inventory(
        _command(
            inventory_env,
            operation_id="op-1",
            quantity_before=base_quantity,
            expected_version=base_version,
        )
    )
    assert receipt.status is WriteStatus.APPLIED
    assert receipt.before_quantity == base_quantity
    assert receipt.after_quantity == base_quantity + 5
    assert receipt.resource_version == base_version + 1


async def test_same_operation_does_not_apply_delta_twice(
    inventory_env: InventoryEnv,
) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    command = _command(
        inventory_env,
        operation_id="op-1",
        quantity_before=inventory_env.level.available_quantity,
        expected_version=inventory_env.level.version,
    )
    first = await adapter.adjust_inventory(command)
    replay = await adapter.adjust_inventory(command)
    assert replay == first
    assert await adapter.count_logical_writes("op-1") == 1


async def test_mock_reconcile_inventory_recovers_after_response_loss(
    inventory_env: InventoryEnv,
) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = inventory_env.level.available_quantity
    adapter.faults.inject("response_lost_after_commit")
    receipt = await adapter.adjust_inventory(
        _command(
            inventory_env,
            operation_id="op-1",
            quantity_before=base_quantity,
            expected_version=inventory_env.level.version,
        )
    )
    assert receipt.status is WriteStatus.UNKNOWN
    assert receipt.before_quantity is None and receipt.after_quantity is None

    adapter.faults.clear()
    result = await adapter.reconcile_inventory(
        _reconcile(inventory_env, operation_id="op-1", quantity_before=base_quantity)
    )
    assert result is ReconcileResult.APPLIED


async def test_mock_reconcile_inventory_not_applied_before_write(
    inventory_env: InventoryEnv,
) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    result = await adapter.reconcile_inventory(
        _reconcile(
            inventory_env,
            operation_id="op-absent",
            quantity_before=inventory_env.level.available_quantity,
        )
    )
    assert result is ReconcileResult.NOT_APPLIED


async def test_mock_reconcile_inventory_returns_unknown_on_concurrent_change(
    inventory_env: InventoryEnv,
) -> None:
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = inventory_env.level.available_quantity
    receipt = await adapter.adjust_inventory(
        _command(
            inventory_env,
            operation_id="op-1",
            quantity_before=base_quantity,
            expected_version=inventory_env.level.version,
        )
    )
    assert receipt.status is WriteStatus.APPLIED

    # 模拟并发变更:直接以 CAS 把数量从 base+5 再改 +5。
    repository = ProductRepository(inventory_env.session)
    level = await repository.get_inventory_level(
        inventory_env.workspace_id, inventory_env.location.id, inventory_env.product.id
    )
    assert level is not None and level.available_quantity == base_quantity + 5
    adjusted = await repository.adjust_inventory(
        workspace_id=inventory_env.workspace_id,
        inventory_level_id=level.id,
        delta=5,
        expected_version=level.version,
    )
    assert adjusted is not None
    await inventory_env.session.commit()

    result = await adapter.reconcile_inventory(
        _reconcile(inventory_env, operation_id="op-1", quantity_before=base_quantity)
    )
    assert result is ReconcileResult.UNKNOWN


# ---------------------------------------------------------------------------
# 回查绑定:错位置 / 错商品 / 错 Workspace 一律 fail closed
# ---------------------------------------------------------------------------


async def _apply_delta(
    adapter: MockCommerceAdapter, env: InventoryEnv, *, operation_id: str, delta: int = 5
) -> int:
    """用给定 Adapter 执行一次增量调整并返回调整前数量。

    幂等去重表是 Adapter 的内存状态,因此调整与回查必须复用**同一个** Adapter
    实例,否则回查看到的是空表(会被误当成"未应用")。
    """
    base_quantity = env.level.available_quantity
    receipt = await adapter.adjust_inventory(
        _command(
            env,
            operation_id=operation_id,
            quantity_before=base_quantity,
            expected_version=env.level.version,
            delta=delta,
        )
    )
    assert receipt.status is WriteStatus.APPLIED
    return base_quantity


async def test_reconcile_rejects_other_location_with_same_quantity(
    inventory_env: InventoryEnv,
) -> None:
    """错位置但目标数量恰好相同 → 不得判定 APPLIED。

    旧实现只比较 ``applied_quantity``,再按回查命令读到"另一个位置数量相同"
    就会误判 APPLIED;绑定资源身份后必须 fail closed。
    """
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")

    other_location = await add_active_location(
        inventory_env.session,
        inventory_env.workspace_id,
        inventory_env.store.id,
        name="二号仓",
    )
    inventory_env.session.add(
        InventoryLevel(
            id=uuid.uuid4(),
            workspace_id=inventory_env.workspace_id,
            location_id=other_location.id,
            product_id=inventory_env.product.id,
            available_quantity=base_quantity + 5,
        )
    )
    await inventory_env.session.commit()

    # 陷阱成立性自证:错位置的库存数量恰等于预期,只比数量会误判 APPLIED。
    twin = await ProductRepository(inventory_env.session).get_inventory_level(
        inventory_env.workspace_id, other_location.id, inventory_env.product.id
    )
    assert twin is not None and twin.available_quantity == base_quantity + 5

    misrouted = dataclasses.replace(
        _reconcile(inventory_env, operation_id="op-1", quantity_before=base_quantity),
        location_id=other_location.id,
        external_location_ref=other_location.external_location_ref,
    )
    assert await adapter.reconcile_inventory(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reconcile_rejects_other_product_with_same_quantity(
    inventory_env: InventoryEnv,
) -> None:
    """错商品但目标数量恰好相同 → 不得判定 APPLIED。"""
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")

    other_product = Product(
        id=uuid.uuid4(),
        workspace_id=inventory_env.workspace_id,
        sku=f"SKU-{uuid.uuid4()}",
        title="twin-product",
    )
    inventory_env.session.add(other_product)
    await inventory_env.session.flush()
    inventory_env.session.add(
        InventoryLevel(
            id=uuid.uuid4(),
            workspace_id=inventory_env.workspace_id,
            location_id=inventory_env.location.id,
            product_id=other_product.id,
            available_quantity=base_quantity + 5,
        )
    )
    await inventory_env.session.commit()

    # 陷阱成立性自证:错商品的库存数量恰等于预期,只比数量会误判 APPLIED。
    twin = await ProductRepository(inventory_env.session).get_inventory_level(
        inventory_env.workspace_id, inventory_env.location.id, other_product.id
    )
    assert twin is not None and twin.available_quantity == base_quantity + 5

    misrouted = dataclasses.replace(
        _reconcile(inventory_env, operation_id="op-1", quantity_before=base_quantity),
        product_id=other_product.id,
    )
    assert await adapter.reconcile_inventory(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reconcile_rejects_other_workspace(
    inventory_env: InventoryEnv,
) -> None:
    """错 Workspace → 不得判定 APPLIED(锁定 fail-closed 语义)。"""
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")

    misrouted = dataclasses.replace(
        _reconcile(inventory_env, operation_id="op-1", quantity_before=base_quantity),
        workspace_id=uuid.uuid4(),
    )
    assert await adapter.reconcile_inventory(misrouted) is ReconcileResult.NOT_APPLIED


async def test_reconcile_applies_only_for_the_recorded_target(
    inventory_env: InventoryEnv,
) -> None:
    """对照面:同一 operation_id + 记录的资源身份 → 仍正常返回 APPLIED。"""
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")

    result = await adapter.reconcile_inventory(
        _reconcile(inventory_env, operation_id="op-1", quantity_before=base_quantity)
    )
    assert result is ReconcileResult.APPLIED


async def test_reused_operation_id_on_another_location_fails_closed(
    inventory_env: InventoryEnv,
) -> None:
    """同一 operation_id 作用到另一个位置 → 确定失败,不回放旧成功回执。

    写路径与回查路径同样要抵御错路由:同一 ``operation_id`` 换了资源目标时,
    回放旧回执会把"另一处已成功"伪装成"本次已生效"。
    """
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")

    other_location = await add_active_location(
        inventory_env.session,
        inventory_env.workspace_id,
        inventory_env.store.id,
        name="二号仓",
    )
    await inventory_env.session.commit()

    conflicting = await adapter.adjust_inventory(
        dataclasses.replace(
            _command(
                inventory_env,
                operation_id="op-1",
                quantity_before=base_quantity,
                expected_version=inventory_env.level.version,
            ),
            location_id=other_location.id,
            external_location_ref=other_location.external_location_ref,
        )
    )

    assert conflicting.status is WriteStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    # 冲突不得产生第二次逻辑写。
    assert await adapter.count_logical_writes("op-1") == 1


@pytest.mark.parametrize(
    ("delta", "reason"),
    [(10, "RECEIPT"), (5, "DAMAGE")],
)
async def test_reused_operation_id_with_different_intent_fails_closed(
    inventory_env: InventoryEnv, delta: int, reason: str
) -> None:
    """同一 operation_id、同一资源,但改了增量或原因 → 确定失败,不回放旧成功。

    只比较资源身份的旧实现会把"另一个业务动作"回放成本次成功;写路径必须比对
    业务意图指纹(delta / expected_quantity_after / reason 都在其中)。
    """
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")

    conflicting = await adapter.adjust_inventory(
        dataclasses.replace(
            _command(
                inventory_env,
                operation_id="op-1",
                quantity_before=base_quantity,
                expected_version=inventory_env.level.version,
            ),
            delta=delta,
            reason=reason,
        )
    )

    assert conflicting.status is WriteStatus.FAILED
    assert conflicting.reason_code == REASON_IDEMPOTENCY_CONFLICT
    assert await adapter.count_logical_writes("op-1") == 1


async def test_duplicate_submit_with_reread_version_still_replays(
    inventory_env: InventoryEnv,
) -> None:
    """对照面:意图相同、仅 expected_version 因重读变化的重复提交仍回放旧回执。"""
    adapter = MockCommerceAdapter(inventory_env.session)
    base_quantity = await _apply_delta(adapter, inventory_env, operation_id="op-1")
    replay_command = _command(
        inventory_env,
        operation_id="op-1",
        quantity_before=base_quantity,
        expected_version=inventory_env.level.version + 1,
    )

    replay = await adapter.adjust_inventory(replay_command)

    assert replay.status is WriteStatus.APPLIED
    assert await adapter.count_logical_writes("op-1") == 1

