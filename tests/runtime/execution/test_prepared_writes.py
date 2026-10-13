"""写 Tool 的 prepare/execute 两阶段契约测试(P1 Task 2 与 Task 2.1)。

不变式:

- ``prepare()`` 只做服务端补全与最终业务校验,不产生外部副作用;
- 可执行写入必须把 Operation 推进 ``EXECUTING``,并在同一次提交里落库完整的
  ``expected_state`` 与执行能力(adapter / 原生幂等 / 回查),然后才发外部调用;
- ``expected_state IS NULL`` 只允许在"从未发起外部调用"的状态(PREPARED /
  NOT_APPLIED)下首次绑定业务目标,其余状态一律 fail closed;
- 复用同一 ``operation_id`` 时不得改变业务目标,也不得改写执行能力;
- prepare 阶段的确定性拒绝直接落 ``NOT_APPLIED``:不进入 ``EXECUTING``、不写
  执行允许审计、不新增 EXECUTE Attempt、Adapter 零调用——恢复方不会把它误判
  为"可能已执行"。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.audit.service import AuditService
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.operations import OperationConflictError
from runtime.execution.tool_executor import (
    ExecutedToolCall,
    PreparedToolWrite,
    PreparedWriteRejection,
    ToolExecutor,
)
from runtime.execution.tool_result_processor import ToolExecutionReceipt, ToolResultProcessor
from runtime.persistence.base import OperationStatus
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
    ToolDefinition,
    ToolVersion,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import Session as SessionRow


TEST_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}

EXPECTED_STATE: dict[str, Any] = {
    "operation_type": "SET_LISTING_PRICE",
    "target_price": "99.00",
}

ADAPTER_KEY: dict[str, str] = {"key": "test-adapter"}
"""夹具执行 Profile 的 adapter_key;集中一处避免多处字面量漂移。"""


class RecordingPreparedHandler:
    """记录外部调用时刻数据库中可见的 Operation 快照。

    读取走独立连接:只有真正提交过的状态才可见,进程内缓存不算通过。
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        expected_state: Mapping[str, Any] | None,
        rejection: PreparedWriteRejection | None = None,
    ) -> None:
        self._engine = engine
        self._expected_state = dict(expected_state) if expected_state is not None else None
        self._rejection = rejection
        self.observed_before_execute: dict[str, Any] | None = None
        self.prepare_calls = 0
        self.execute_prepared_calls = 0

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        del normalized_arguments
        self.prepare_calls += 1
        if self._rejection is not None:
            return PreparedToolWrite(
                command=None,
                expected_state=None,
                request_fingerprint="fingerprint-rejected",
                rejection=self._rejection,
            )
        return PreparedToolWrite(
            command=object(),
            expected_state=dict(self._expected_state or {}),
            request_fingerprint="fingerprint-1",
        )

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        del idempotency_key, prepared
        self.execute_prepared_calls += 1
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            row = (
                await session.scalars(
                    select(Operation).where(Operation.operation_id == operation_id)
                )
            ).one()
            self.observed_before_execute = {
                "status": row.status.value,
                "expected_state": (
                    dict(row.expected_state) if row.expected_state is not None else None
                ),
                "adapter_key": row.adapter_key,
                "supports_native_idempotency": row.supports_native_idempotency,
                "supports_reconciliation": row.supports_reconciliation,
            }
        finally:
            await session.close()
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id, status="APPLIED", payload={"result": "ok"}
        )


@dataclass(frozen=True)
class RuntimeWriteFixture:
    """绑定已提交种子与真实短事务的执行夹具。"""

    engine: AsyncEngine
    session: AsyncSession
    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    run_id: uuid.UUID
    tool_call_id: uuid.UUID
    lease_token: uuid.UUID
    state_version: int

    async def execute(self, *, handler: Any) -> ExecutedToolCall:
        """以提交型 checkpoint 执行一次两阶段写。"""

        async def checkpoint() -> None:
            await self.session.commit()

        executor = ToolExecutor(
            self.session,
            workspace_id=self.workspace_id,
            actor_id=self.actor_id,
            run_id=self.run_id,
            lease_token=self.lease_token,
            expected_state_version=self.state_version,
            handler=handler,
            processor=ToolResultProcessor(output_schema=TEST_OUTPUT_SCHEMA),
            execution_profile=ToolExecutionProfile(
                adapter_key=ADAPTER_KEY["key"],
                supports_native_idempotency=True,
                supports_reconciliation=True,
            ),
            output_schema=TEST_OUTPUT_SCHEMA,
            checkpoint=checkpoint,
        )
        return await executor.execute(
            tool_call_id=self.tool_call_id, normalized_arguments={"listing_id": "L1"}
        )


@pytest.fixture()
async def runtime_write_fixture(
    engine: AsyncEngine,
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    leased_run,
) -> RuntimeWriteFixture:
    """提交种子与租约后,给出可在真实短事务中执行的写夹具。"""
    tool_version = await db_session.get(ToolVersion, seeded_write_tool_call.tool_version_id)
    assert tool_version is not None
    cleanup_ids = {
        "workspace_id": seeded_workspace.workspace.id,
        "user_id": seeded_workspace.user.id,
        "membership_id": seeded_workspace.membership.id,
        "agent_id": seeded_workspace.agent.id,
        "agent_version_id": seeded_workspace.agent_version.id,
        "model_connection_id": seeded_workspace.model_config_version.model_connection_id,
        "model_config_version_id": seeded_workspace.model_config_version.id,
        "session_id": seeded_run.session_id,
        "run_id": seeded_run.id,
        "tool_call_id": seeded_write_tool_call.id,
        "tool_version_id": tool_version.id,
        "tool_definition_id": tool_version.tool_id,
    }
    await db_session.commit()
    session = AsyncSession(engine, expire_on_commit=False)
    fixture = RuntimeWriteFixture(
        engine=engine,
        session=session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=seeded_run.id,
        tool_call_id=seeded_write_tool_call.id,
        lease_token=leased_run.lease_token,
        state_version=leased_run.state_version,
    )
    try:
        yield fixture
    finally:
        await session.close()
        await _cleanup_committed_seed(engine, cleanup_ids)


async def _load_operation(engine: AsyncEngine, operation_id: uuid.UUID) -> Operation:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                select(Operation).where(Operation.operation_id == operation_id)
            )
        ).one()
    finally:
        await session.close()


async def _seed_operation(
    engine: AsyncEngine,
    *,
    workspace_id: uuid.UUID,
    tool_call_id: uuid.UUID,
    operation_id: uuid.UUID,
    status: OperationStatus,
    expected_state: Mapping[str, Any] | None,
    adapter_key: str = "test-adapter",
    supports_native_idempotency: bool = False,
    supports_reconciliation: bool = False,
) -> None:
    """预置一条已存在的 Operation 行并把 operation_id 写回 ToolCall。

    模拟真实路径:确认前已被 ``_assign_operation_id`` 预约(PREPARED),或历史
    遗留/异常状态(EXECUTING 但没有目标记录)。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add(
            Operation(
                workspace_id=workspace_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=f"op:{operation_id}",
                adapter_key=adapter_key,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
                expected_state=dict(expected_state) if expected_state is not None else None,
                status=status,
            )
        )
        await session.flush()
        await session.execute(
            sa.update(ToolCall)
            .where(ToolCall.workspace_id == workspace_id, ToolCall.id == tool_call_id)
            .values(operation_id=operation_id)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    finally:
        await session.close()


async def _execute_attempts(engine: AsyncEngine, operation_row_id: uuid.UUID) -> list[str]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        rows = (
            await session.scalars(
                select(OperationAttempt).where(
                    OperationAttempt.operation_row_id == operation_row_id
                )
            )
        ).all()
        return [row.kind.value for row in rows]
    finally:
        await session.close()


async def _uncertain_operations(engine: AsyncEngine, workspace_id: uuid.UUID) -> list[str]:
    """按 LeaseSweeper 的判据列出"可能已执行"的 Operation(EXECUTING/UNKNOWN)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        rows = (
            await session.scalars(
                select(Operation.operation_id).where(
                    Operation.workspace_id == workspace_id,
                    Operation.status.in_(
                        (OperationStatus.EXECUTING, OperationStatus.UNKNOWN)
                    ),
                )
            )
        ).all()
        return [str(row) for row in rows]
    finally:
        await session.close()


async def _audit_actions(engine: AsyncEngine, *, workspace_id: uuid.UUID, actor_id: uuid.UUID,
                         run_id: uuid.UUID) -> list[str]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        audit = AuditService(session, workspace_id=workspace_id, actor_id=actor_id)
        events = await audit.list_for_run(workspace_id=workspace_id, run_id=run_id)
        return [event.action for event in events]
    finally:
        await session.close()


async def _cleanup_committed_seed(engine: AsyncEngine, ids: dict[str, Any]) -> None:
    """按外键逆序清理已提交种子行(test_operations 同构)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        ws = ids["workspace_id"]
        await session.execute(sa.text("DELETE FROM operation_attempts WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM agent_events WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM audit_events WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM run_jobs WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM run_attempts WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(
            sa.text("UPDATE tool_calls SET operation_id = NULL WHERE workspace_id = :ws"),
            {"ws": ws},
        )
        await session.execute(sa.text("DELETE FROM operations WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM tool_calls WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM messages WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.delete(ToolVersion).where(ToolVersion.id == ids["tool_version_id"]))
        await session.execute(
            sa.delete(ToolDefinition).where(ToolDefinition.id == ids["tool_definition_id"])
        )
        await session.execute(sa.delete(Run).where(Run.workspace_id == ws))
        await session.execute(sa.delete(SessionRow).where(SessionRow.workspace_id == ws))
        await session.execute(
            sa.delete(ModelConfigVersion).where(
                ModelConfigVersion.id == ids["model_config_version_id"]
            )
        )
        await session.execute(
            sa.delete(ModelConnection).where(ModelConnection.id == ids["model_connection_id"])
        )
        await session.execute(
            sa.delete(AgentVersion).where(AgentVersion.id == ids["agent_version_id"])
        )
        await session.execute(sa.delete(Agent).where(Agent.id == ids["agent_id"]))
        await session.execute(
            sa.delete(WorkspaceMembership).where(WorkspaceMembership.id == ids["membership_id"])
        )
        await session.execute(sa.delete(User).where(User.id == ids["user_id"]))
        await session.execute(sa.delete(Workspace).where(Workspace.id == ws))
        await session.commit()
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# 可执行准备:expected_state 与执行能力先于外部调用落库
# ---------------------------------------------------------------------------


async def test_expected_state_is_committed_before_external_write(
    runtime_write_fixture: RuntimeWriteFixture,
) -> None:
    """expected_state 先于外部调用落库并提交,且此时 Operation 已是 EXECUTING。"""
    handler = RecordingPreparedHandler(
        engine=runtime_write_fixture.engine, expected_state=EXPECTED_STATE
    )
    outcome = await runtime_write_fixture.execute(handler=handler)

    assert handler.observed_before_execute is not None
    assert handler.observed_before_execute["expected_state"] == EXPECTED_STATE
    assert handler.observed_before_execute["status"] == "EXECUTING"
    assert outcome.begun is not None
    persisted = await _load_operation(runtime_write_fixture.engine, outcome.begun.operation_id)
    assert persisted.status is OperationStatus.APPLIED
    assert persisted.expected_state == EXPECTED_STATE


async def test_prepared_operation_is_promoted_to_executing_with_capabilities(
    engine: AsyncEngine, runtime_write_fixture: RuntimeWriteFixture
) -> None:
    """真实确认路径:PREPARED 行被首次绑定目标并推进 EXECUTING,能力同时落库。"""
    operation_id = uuid.uuid4()
    # 确认前的预约行:只有 operation_id 与 adapter_key,没有目标,能力是占位值。
    await _seed_operation(
        engine,
        workspace_id=runtime_write_fixture.workspace_id,
        tool_call_id=runtime_write_fixture.tool_call_id,
        operation_id=operation_id,
        status=OperationStatus.PREPARED,
        expected_state=None,
    )
    handler = RecordingPreparedHandler(engine=engine, expected_state=EXPECTED_STATE)
    outcome = await runtime_write_fixture.execute(handler=handler)

    assert outcome.begun is not None
    assert outcome.begun.operation_id == operation_id
    assert outcome.begun.created is False
    # 外部调用发生时,提交过的行必须是 EXECUTING + 完整目标 + 真实执行能力。
    assert handler.observed_before_execute == {
        "status": "EXECUTING",
        "expected_state": EXPECTED_STATE,
        "adapter_key": ADAPTER_KEY["key"],
        "supports_native_idempotency": True,
        "supports_reconciliation": True,
    }
    persisted = await _load_operation(engine, operation_id)
    assert persisted.expected_state == EXPECTED_STATE
    assert persisted.supports_native_idempotency is True
    assert persisted.supports_reconciliation is True


async def test_reuse_of_operation_id_rejects_changed_expected_state(
    runtime_write_fixture: RuntimeWriteFixture,
) -> None:
    """同一 operation_id 不得改变业务预期。"""
    first = RecordingPreparedHandler(
        engine=runtime_write_fixture.engine, expected_state=EXPECTED_STATE
    )
    await runtime_write_fixture.execute(handler=first)

    second = RecordingPreparedHandler(
        engine=runtime_write_fixture.engine,
        expected_state={**EXPECTED_STATE, "target_price": "88.00"},
    )
    with pytest.raises(OperationConflictError):
        await runtime_write_fixture.execute(handler=second)


# ---------------------------------------------------------------------------
# 状态机 fail closed:目标记录缺失与终态重入
# ---------------------------------------------------------------------------


async def test_executing_operation_without_expected_state_fails_closed(
    engine: AsyncEngine, runtime_write_fixture: RuntimeWriteFixture
) -> None:
    """EXECUTING 却没有目标记录:拒绝执行,绝不按"首次绑定"放行。"""
    operation_id = uuid.uuid4()
    await _seed_operation(
        engine,
        workspace_id=runtime_write_fixture.workspace_id,
        tool_call_id=runtime_write_fixture.tool_call_id,
        operation_id=operation_id,
        status=OperationStatus.EXECUTING,
        expected_state=None,
        adapter_key=ADAPTER_KEY["key"],
    )
    handler = RecordingPreparedHandler(engine=engine, expected_state=EXPECTED_STATE)

    with pytest.raises(OperationConflictError, match="expected_state 为空"):
        await runtime_write_fixture.execute(handler=handler)

    assert handler.execute_prepared_calls == 0
    persisted = await _load_operation(engine, operation_id)
    assert persisted.status is OperationStatus.EXECUTING
    assert persisted.expected_state is None


async def test_changed_execution_capabilities_fail_closed(
    engine: AsyncEngine, runtime_write_fixture: RuntimeWriteFixture
) -> None:
    """已绑定目标的 Operation 不得在执行能力上被改写(幂等/回查不一致即拒绝)。"""
    operation_id = uuid.uuid4()
    await _seed_operation(
        engine,
        workspace_id=runtime_write_fixture.workspace_id,
        tool_call_id=runtime_write_fixture.tool_call_id,
        operation_id=operation_id,
        status=OperationStatus.EXECUTING,
        expected_state=EXPECTED_STATE,
        adapter_key=ADAPTER_KEY["key"],
        supports_native_idempotency=False,
        supports_reconciliation=False,
    )
    handler = RecordingPreparedHandler(engine=engine, expected_state=EXPECTED_STATE)

    with pytest.raises(OperationConflictError, match="执行能力不一致"):
        await runtime_write_fixture.execute(handler=handler)

    assert handler.execute_prepared_calls == 0


async def test_terminal_operation_cannot_reenter_execution(
    engine: AsyncEngine, runtime_write_fixture: RuntimeWriteFixture
) -> None:
    """APPLIED 的 Operation 不得再次进入执行边界(重复写防线)。"""
    operation_id = uuid.uuid4()
    await _seed_operation(
        engine,
        workspace_id=runtime_write_fixture.workspace_id,
        tool_call_id=runtime_write_fixture.tool_call_id,
        operation_id=operation_id,
        status=OperationStatus.APPLIED,
        expected_state=EXPECTED_STATE,
        adapter_key=ADAPTER_KEY["key"],
    )
    handler = RecordingPreparedHandler(engine=engine, expected_state=EXPECTED_STATE)

    with pytest.raises(OperationConflictError, match="不允许进入执行边界"):
        await runtime_write_fixture.execute(handler=handler)

    assert handler.execute_prepared_calls == 0


# ---------------------------------------------------------------------------
# 确定性拒绝:不进执行窗口
# ---------------------------------------------------------------------------


async def test_prepared_rejection_is_audited_without_touching_adapter(
    runtime_write_fixture: RuntimeWriteFixture,
) -> None:
    """确定性拒绝:落 NOT_APPLIED,Adapter 零调用。"""
    handler = RecordingPreparedHandler(
        engine=runtime_write_fixture.engine,
        expected_state=None,
        rejection=PreparedWriteRejection(
            status="FAILED",
            error_code="RESOURCE_CHANGED",
            context={"operation_type": "SET_LISTING_PRICE", "store_id": "S1"},
        ),
    )
    outcome = await runtime_write_fixture.execute(handler=handler)

    assert handler.execute_prepared_calls == 0
    assert outcome.begun is None
    assert outcome.recorded.operation_outcome.value == "NOT_APPLIED"
    assert outcome.recorded.tool_call_status.value == "VALIDATING"
    assert outcome.processed.tool_call_error == "RESOURCE_CHANGED"
    persisted = await _load_operation(runtime_write_fixture.engine, outcome.recorded.operation_id)
    assert persisted.status is OperationStatus.NOT_APPLIED
    # 拒绝不存在可回查的业务目标:expected_state 保持 NULL,审计上下文另存。
    assert persisted.expected_state is None
    assert persisted.last_receipt is not None
    assert persisted.last_receipt["rejection_context"] == {
        "operation_type": "SET_LISTING_PRICE",
        "store_id": "S1",
    }


async def test_rejection_does_not_open_an_execution_window(
    runtime_write_fixture: RuntimeWriteFixture,
) -> None:
    """拒绝不产生不确定窗口:无 EXECUTING、无 EXECUTE Attempt、无执行允许审计。

    这三点合起来保证:即使进程在拒绝之后立即崩溃,恢复方也不会把该 Operation
    判为"可能已执行",因此不会有任何回查被无谓触发。
    """
    handler = RecordingPreparedHandler(
        engine=runtime_write_fixture.engine,
        expected_state=None,
        rejection=PreparedWriteRejection(
            status="FAILED", error_code="RESOURCE_CHANGED", context={"sku": "SKU-001"}
        ),
    )
    outcome = await runtime_write_fixture.execute(handler=handler)

    assert outcome.begun is None
    persisted = await _load_operation(runtime_write_fixture.engine, outcome.recorded.operation_id)
    assert await _execute_attempts(runtime_write_fixture.engine, persisted.id) == []
    assert await _uncertain_operations(runtime_write_fixture.engine, persisted.workspace_id) == []
    actions = await _audit_actions(
        runtime_write_fixture.engine,
        workspace_id=runtime_write_fixture.workspace_id,
        actor_id=runtime_write_fixture.actor_id,
        run_id=runtime_write_fixture.run_id,
    )
    assert "TOOL_WRITE_REJECTED" in actions
    assert "TOOL_WRITE_ALLOWED" not in actions


async def test_rejection_on_reserved_operation_keeps_expected_state_unbound(
    engine: AsyncEngine, runtime_write_fixture: RuntimeWriteFixture
) -> None:
    """确认前已预约的 Operation 被拒绝后不得被误标为"已绑定目标"。"""
    operation_id = uuid.uuid4()
    await _seed_operation(
        engine,
        workspace_id=runtime_write_fixture.workspace_id,
        tool_call_id=runtime_write_fixture.tool_call_id,
        operation_id=operation_id,
        status=OperationStatus.PREPARED,
        expected_state=None,
    )
    handler = RecordingPreparedHandler(
        engine=engine,
        expected_state=None,
        rejection=PreparedWriteRejection(
            status="FAILED", error_code="LISTING_NOT_FOUND", context={"sku": "SKU-001"}
        ),
    )
    outcome = await runtime_write_fixture.execute(handler=handler)

    assert outcome.begun is None
    assert outcome.recorded.operation_id == operation_id
    persisted = await _load_operation(engine, operation_id)
    assert persisted.status is OperationStatus.NOT_APPLIED
    assert persisted.expected_state is None
    assert handler.execute_prepared_calls == 0
