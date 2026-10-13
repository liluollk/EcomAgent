"""端到端改价闭环测试(计划 03 Task 6;架构文档 §5、§6、§7)。

``product_application`` 夹具组装完整 Runtime(真库,参照 crash_harness
模式)+ ProductOperationsSeeder 种子;``start`` 包装 RunFactory → ADVANCE_RUN
Job → Runtime Worker 推进;``confirm`` 用 ConfirmationService.accept +
恢复推进。模型流由确定性 ScriptedAgentBackend 脚本产生 update_price
ToolCall(店铺标识使用 ``str(store.id)``,与 Task 2 只读 Tool 约定一致)。

覆盖分支:
- 信息完整(用户文本含精确店铺证据)→ 直接 WAITING_CONFIRMATION →
  confirm → COMPLETED,权威价格经 MockCommerceAdapter 真实写 listings;
- 缺店铺 → WAITING_INPUT → answer 补店铺 → 确认 → 完成;
- 低于底价 → Policy DENY:ToolCall DENIED、无确认、权威状态不变;
- 响应丢失(写入已生效)→ WAITING_RECONCILIATION → reconcile APPLIED →
  完成,且仅一次逻辑写;
- 重复提交同一 operation_id → 平台幂等回放,逻辑写不增加。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.persistence.models import Listing
from runtime.backend.protocol import BackendCompleted, ToolCallCompleted
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import OperationStatus
from runtime.persistence.models.execution import Operation


async def test_confirmed_price_update_reaches_authoritative_state(
    product_application,
) -> None:
    """计划原文用例:完整确认链路后权威状态为 99.00。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    assert run.pending_confirmation_id is not None

    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED
    assert (
        await product_application.authoritative_price(store_id, "SKU-001")
        == Decimal("99.00")
    )

    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    assert snapshot.status == "SUCCEEDED"
    assert snapshot.operation_id is not None


async def test_price_gateway_is_called_without_open_database_transaction(
    product_application,
) -> None:
    """Listing 读取事务须在平台 Gateway 请求前提交。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )
    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION

    original_gateway = product_application._write_handler._gateway

    class TransactionCheckingGateway:
        async def execute(self, command):
            assert not product_application._adapter_session.in_transaction()
            return await original_gateway.execute(command)

    product_application._write_handler._gateway = TransactionCheckingGateway()
    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED


async def test_missing_store_asks_input_then_completes_after_answer(
    product_application,
) -> None:
    """缺店铺分支:WAITING_INPUT → answer 补店铺(USER_CONFIRMED)→ 确认 → 完成。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [[_update_price_call(None, "SKU-001", 99), BackendCompleted(finish_reason="tool_calls")]]
    )

    run = await product_application.start("把 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_INPUT
    assert run.pending_input_id is not None

    run = await product_application.answer(
        run.pending_input_id, {"store_id": store_id}
    )
    assert run.status == RunStatus.WAITING_CONFIRMATION

    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED
    assert (
        await product_application.authoritative_price(store_id, "SKU-001")
        == Decimal("99.00")
    )


async def test_below_floor_price_is_denied_without_confirmation_or_write(
    product_application,
) -> None:
    """低于底价分支:Policy DENY,ToolCall DENIED,权威状态不变、无确认请求。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 30)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 30 元")
    assert run.status == RunStatus.COMPLETED
    assert run.pending_confirmation_id is None

    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    assert snapshot.status == "DENIED"
    assert snapshot.failure_code == "PRICE_BELOW_FLOOR"
    assert snapshot.operation_id is None
    assert (
        await product_application.authoritative_price(store_id, "SKU-001")
        == Decimal("128.00")
    )


async def test_response_lost_reconciles_to_applied_without_second_write(
    product_application,
) -> None:
    """响应丢失分支:写入已生效但回执 UNKNOWN → 回查 APPLIED → 完成,仅一次写。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.adapter.faults.inject("response_lost_after_commit")
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION

    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.WAITING_RECONCILIATION
    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    operation_id = snapshot.operation_id
    assert operation_id is not None

    run = await product_application.recover(run.run_id)
    assert run.status == RunStatus.COMPLETED
    assert (
        await product_application.authoritative_price(store_id, "SKU-001")
        == Decimal("99.00")
    )
    assert await product_application.adapter.count_logical_writes(str(operation_id)) == 1


async def test_reconciliation_adapter_is_called_without_runtime_transaction(
    product_application,
) -> None:
    """Adapter 回查等待期间不得持有 Runtime 状态事务。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.adapter.faults.inject("response_lost_after_commit")
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )
    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.WAITING_RECONCILIATION

    original_factory = product_application._session_factory
    reconciliation_sessions = []

    def tracked_factory():
        session = original_factory()
        reconciliation_sessions.append(session)
        return session

    original_reconcile = product_application.adapter.reconcile

    async def transaction_checking_reconcile(command):
        assert reconciliation_sessions
        assert all(not session.in_transaction() for session in reconciliation_sessions)
        return await original_reconcile(command)

    product_application._session_factory = tracked_factory
    product_application.adapter.reconcile = transaction_checking_reconcile
    run = await product_application.recover(run.run_id)
    assert run.status == RunStatus.COMPLETED
    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    assert snapshot.status == "SUCCEEDED"


async def test_duplicate_submit_with_same_operation_id_is_idempotent(
    product_application,
) -> None:
    """重复提交分支:同一 operation_id 再度执行 → 平台回放回执,逻辑写不增加。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED

    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    operation_id = snapshot.operation_id
    assert operation_id is not None

    # 以同一 operation_id 重复发起外部执行(平台收到的重复提交)。
    handler = product_application.write_handler
    prepared = await handler.prepare(
        normalized_arguments={"store_id": store_id, "sku": "SKU-001", "target_price": 99}
    )
    replay = await handler.execute_prepared(
        tool_call_id=uuid.uuid4(),
        operation_id=operation_id,
        idempotency_key=f"op:{operation_id}",
        prepared=prepared,
    )
    assert replay.status == "APPLIED"
    assert await product_application.adapter.count_logical_writes(str(operation_id)) == 1
    assert (
        await product_application.authoritative_price(store_id, "SKU-001")
        == Decimal("99.00")
    )


async def test_listing_version_conflict_after_restart(
    product_application,
    rebuild_product_application,
    engine: AsyncEngine,
) -> None:
    """重启后确认:持久化的资源版本与当前 Listing.version 不一致 → 执行前拒绝。

    旧的进程内版本字典在重启后为空,无法发现这类冲突;本用例锁定"资源版本
    随确认请求持久化、重启后仍可比对"的行为。
    """
    ids = product_application.seed_ids
    store_id = str(ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    confirmation_id = run.pending_confirmation_id
    assert confirmation_id is not None

    # 模拟进程重启:关闭旧组合根;确认等待期间 Listing 被其他路径修改。
    await product_application.aclose()
    await _bump_listing_version(engine, ids.listing_id)

    rebuilt = await rebuild_product_application()
    try:
        run = await rebuilt.confirm(confirmation_id)
        snapshot = await rebuilt.tool_call_snapshot(run.run_id)
    finally:
        await rebuilt.aclose()

    assert run.status == RunStatus.COMPLETED
    assert snapshot is not None
    assert snapshot.status == "DENIED"
    assert snapshot.failure_code == "LISTING_VERSION_CONFLICT"
    # 拒绝发生在外部写入前:权威价格仍是种子值。
    assert (
        await product_application.authoritative_price(store_id, "SKU-001")
        == Decimal("128.00")
    )


async def test_reserved_operation_is_promoted_on_confirmed_execution(
    product_application, engine: AsyncEngine
) -> None:
    """真实确认路径:预约的 PREPARED Operation 在执行前被绑定目标并推进。

    锁定 Task 2.1:确认前只预约 ``operation_id``(无目标、能力是占位值),真正
    执行时才同时落库完整 ``expected_state`` 与该写 Tool 的执行能力。
    """
    ids = product_application.seed_ids
    store_id = str(ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_update_price_round(store_id, "SKU-001", 99)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    operation_id = snapshot.operation_id
    assert operation_id is not None

    reserved = await _load_operation_row(engine, operation_id)
    assert reserved.status is OperationStatus.PREPARED
    assert reserved.expected_state is None
    # 预约阶段只有占位能力:执行能力必须来自执行边界,而不是确认前的默认值。
    assert reserved.supports_reconciliation is False
    assert reserved.supports_native_idempotency is False

    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED

    executed = await _load_operation_row(engine, operation_id)
    assert executed.status is OperationStatus.APPLIED
    assert executed.expected_state is not None
    assert executed.expected_state["operation_type"] == "UPDATE_PRICE_LEGACY"
    assert executed.expected_state["listing_id"] == str(ids.listing_id)
    assert executed.expected_state["target_price"] == "99.00"
    # 执行能力随首次执行边界落库,并驱动平台回查能力。
    assert executed.adapter_key == reserved.adapter_key
    assert executed.supports_reconciliation is True


async def test_prepare_rejects_unresolvable_listing_with_minimal_context(
    product_application,
) -> None:
    """Listing 无法解析:拒绝不伪造 expected_state,只给最小审计上下文。"""
    prepared = await product_application.write_handler.prepare(
        normalized_arguments={
            "store_id": str(uuid.uuid4()),
            "sku": "SKU-001",
            "target_price": 99,
        }
    )

    assert prepared.command is None
    assert prepared.expected_state is None
    assert prepared.rejection is not None
    assert prepared.rejection.status == "FAILED"
    assert prepared.rejection.error_code == "LISTING_NOT_FOUND"
    # 不臆测 listing_id / currency / expected_version:拒绝上下文只含 PreToolUse
    # 已固定的模型输入与策略版本,不为"形状统一"补空值。
    assert set(prepared.rejection.context) == {
        "operation_type",
        "store_id",
        "sku",
        "target_price",
        "policy_version",
    }


async def _load_operation_row(engine: AsyncEngine, operation_id: uuid.UUID) -> Operation:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                select(Operation).where(Operation.operation_id == operation_id)
            )
        ).one()
    finally:
        await session.close()


async def _bump_listing_version(engine: AsyncEngine, listing_id: uuid.UUID) -> None:
    """模拟外部修改:直接在已提交事务中推进 Listing 版本号。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await session.execute(
                sa.update(Listing)
                .where(Listing.id == listing_id)
                .values(version=Listing.version + 1)
            )
    finally:
        await session.close()


def _update_price_round(store_id: str | None, sku: str, price: int) -> list:
    """模型提出 update_price ToolCall 的一轮脚本。"""
    return [
        _update_price_call(store_id, sku, price),
        BackendCompleted(finish_reason="tool_calls"),
    ]


def _update_price_call(store_id: str | None, sku: str, price: int) -> ToolCallCompleted:
    arguments: dict[str, object] = {"sku": sku, "target_price": price}
    if store_id is not None:
        arguments["store_id"] = store_id
    return ToolCallCompleted(call_id="c0", name="update_price", arguments=arguments)
