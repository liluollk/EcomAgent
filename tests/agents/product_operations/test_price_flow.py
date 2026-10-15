"""端到端改价闭环测试(计划 03 Task 6;架构文档 §5、§6、§7)。

``product_application`` 夹具组装完整 Runtime(真库,参照 crash_harness
模式)+ ProductOperationsSeeder 种子;``start`` 包装 RunFactory → ADVANCE_RUN
Job → Runtime Worker 推进;``confirm`` 用 ConfirmationService.accept +
恢复推进。模型流由确定性 ScriptedAgentBackend 脚本产生 set_listing_price
ToolCall，模型只使用公开的 ``store_ref``，内部 UUID 不进入 Tool 参数。

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
from agents.product_operations.pricing.gateway import SetListingPriceCommand
from runtime.backend.protocol import BackendCompleted, ToolCallCompleted
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import OperationStatus
from runtime.persistence.models.execution import Operation


STORE_REF = "淘宝旗舰店"


async def test_confirmed_price_update_reaches_authoritative_state(
    product_application,
) -> None:
    """计划原文用例:完整确认链路后权威状态为 99.00。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [_set_price_round(STORE_REF, "SKU-001", 99)]
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
    product_application.scripted_backend.set_rounds(
        [_set_price_round(STORE_REF, "SKU-001", 99)]
    )
    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION

    original_set_price = product_application.adapter.set_price

    async def transaction_checking_set_price(command):
        assert not product_application._adapter_session.in_transaction()
        return await original_set_price(command)

    product_application.adapter.set_price = transaction_checking_set_price
    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED


async def test_missing_store_asks_input_then_completes_after_answer(
    product_application,
) -> None:
    """缺店铺分支:WAITING_INPUT → answer 补店铺(USER_CONFIRMED)→ 确认 → 完成。"""
    store_id = str(product_application.seed_ids.store_id)
    product_application.scripted_backend.set_rounds(
        [[_set_price_call(None, "SKU-001", 99), BackendCompleted(finish_reason="tool_calls")]]
    )

    run = await product_application.start("把 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_INPUT
    assert run.pending_input_id is not None

    run = await product_application.answer(
        run.pending_input_id, {"store_ref": STORE_REF}
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
        [_set_price_round(STORE_REF, "SKU-001", 30)]
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
        [_set_price_round(STORE_REF, "SKU-001", 99)]
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
    product_application.adapter.faults.inject("response_lost_after_commit")
    product_application.scripted_backend.set_rounds(
        [_set_price_round(STORE_REF, "SKU-001", 99)]
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

    original_reconcile = product_application.adapter.reconcile_price

    async def transaction_checking_reconcile(command):
        assert reconciliation_sessions
        assert all(not session.in_transaction() for session in reconciliation_sessions)
        return await original_reconcile(command)

    product_application._session_factory = tracked_factory
    product_application.adapter.reconcile_price = transaction_checking_reconcile
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
        [_set_price_round(STORE_REF, "SKU-001", 99)]
    )

    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED

    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    operation_id = snapshot.operation_id
    assert operation_id is not None

    # 以同一 operation_id 重复发起外部执行(平台收到的重复提交)。
    operation = await _load_operation_row(product_application._engine, operation_id)
    expected = operation.expected_state
    assert expected is not None
    replay = await product_application.adapter.set_price(
        SetListingPriceCommand(
            operation_id=str(operation_id),
            idempotency_key=f"set_listing_price:{operation_id}",
            workspace_id=uuid.UUID(expected["workspace_id"]),
            store_id=uuid.UUID(expected["store_id"]),
            listing_id=uuid.UUID(expected["listing_id"]),
            external_store_ref=expected["external_store_ref"],
            external_product_ref=expected["external_product_ref"],
            sku=expected["sku"],
            target_price=Decimal(expected["target_price"]),
            currency=expected["currency"],
            expected_version=int(expected["expected_version"]),
        )
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
        [_set_price_round(STORE_REF, "SKU-001", 99)]
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

    锁定 Task 2.1/Task 2.2:确认前预约即持久化完整执行能力与该写 Tool 的
    adapter_key,真正执行时只补完整 ``expected_state``;执行边界比较能力而不是
    覆盖能力。
    """
    ids = product_application.seed_ids
    product_application.scripted_backend.set_rounds(
        [_set_price_round(STORE_REF, "SKU-001", 99)]
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
    # 预约阶段已带上完整执行能力:执行边界只校验一致性,不接管能力写入。
    assert reserved.supports_reconciliation is True
    assert reserved.supports_native_idempotency is False

    run = await product_application.confirm(run.pending_confirmation_id)
    assert run.status == RunStatus.COMPLETED

    executed = await _load_operation_row(engine, operation_id)
    assert executed.status is OperationStatus.APPLIED
    assert executed.expected_state is not None
    assert executed.expected_state["operation_type"] == "SET_LISTING_PRICE"
    assert executed.expected_state["listing_id"] == str(ids.listing_id)
    assert executed.expected_state["target_price"] == "99.00"
    # 执行能力在预约阶段落库,执行后保持不变。
    assert executed.adapter_key == reserved.adapter_key
    assert executed.supports_reconciliation is True
    assert executed.supports_native_idempotency is False


async def test_prepare_rejects_unresolvable_listing_with_minimal_context(
    product_application,
) -> None:
    """Listing 无法解析:拒绝不伪造 expected_state,只给最小审计上下文。"""
    version_id = product_application.capability_ids["tool_version_ids"][
        "set_listing_price"
    ]
    binding = product_application._tool_registry.binding_for_version(version_id)
    assert binding is not None
    prepared = await binding.handler.prepare(
        normalized_arguments={
            "store_ref": "不存在的店铺",
            "sku": "SKU-001",
            "target_price": 99,
        }
    )

    assert prepared.command is None
    assert prepared.expected_state is None
    assert prepared.rejection is not None
    assert prepared.rejection.status == "FAILED"
    assert prepared.rejection.error_code == "NOT_FOUND"
    # 不臆测 listing_id / currency / expected_version:拒绝上下文只含 PreToolUse
    # 已固定的模型输入与策略版本,不为"形状统一"补空值。
    assert {
        "operation_type",
        "store_ref",
        "sku",
        "target_price",
        "policy_version",
        "rejection_detail",
    } <= set(prepared.rejection.context)


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


def _set_price_round(store_ref: str | None, sku: str, price: int) -> list:
    """模型提出 set_listing_price ToolCall 的一轮脚本。"""
    return [
        _set_price_call(store_ref, sku, price),
        BackendCompleted(finish_reason="tool_calls"),
    ]


def _set_price_call(store_ref: str | None, sku: str, price: int) -> ToolCallCompleted:
    arguments: dict[str, object] = {"sku": sku, "target_price": price}
    if store_ref is not None:
        arguments["store_ref"] = store_ref
    return ToolCallCompleted(
        call_id="c0", name="set_listing_price", arguments=arguments
    )
