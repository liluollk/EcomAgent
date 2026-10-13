"""OperationService 测试:两阶段写登记、结果提交与 fencing(计划 02 Task 8)。

计划原文测试:
- ``test_unknown_write_schedules_reconciliation``:UNKNOWN 结果只能进入
  WAITING_RECONCILIATION,且下一步 Job 是 RECONCILE_OPERATION。
"""

import uuid
from collections.abc import Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.audit.service import AuditService
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.operations import (
    OperationConflictError,
    OperationOutcome,
    OperationResultRecord,
    OperationService,
    audit_write_decision,
)
from runtime.execution.tool_executor import (
    PreparedToolWrite,
    ToolExecutor,
)
from runtime.execution.tool_result_processor import ToolExecutionReceipt, ToolResultProcessor
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    RunJobKind,
    ToolCallStatus,
)
from runtime.persistence.base import (
    RunStatus as PersistenceRunStatus,
)
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
from runtime.persistence.repositories.events import EventRepository


def tool_result_unknown(tool_call_id: uuid.UUID) -> OperationResultRecord:
    """响应丢失:适配器无法给出确定结果 → UNKNOWN(禁止盲目重试)。"""
    return OperationResultRecord(
        tool_call_id=tool_call_id,
        outcome=OperationOutcome.UNKNOWN,
        payload=None,
        error_code="RESPONSE_LOST",
    )


TEST_EXPECTED_STATE: dict[str, Any] = {"operation_type": "TEST_WRITE"}


async def fresh_tool_call(
    session: AsyncSession, workspace_id: uuid.UUID, tool_call_id: uuid.UUID
) -> ToolCall:
    """绕过身份映射重读 ToolCall(条件更新不走 ORM 同步)。"""
    stmt = (
        select(ToolCall)
        .where(ToolCall.workspace_id == workspace_id, ToolCall.id == tool_call_id)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def fresh_operation(
    session: AsyncSession, workspace_id: uuid.UUID, operation_id: uuid.UUID
) -> Operation:
    stmt = (
        select(Operation)
        .where(Operation.workspace_id == workspace_id, Operation.operation_id == operation_id)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def fresh_run(session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID) -> Run:
    stmt = (
        select(Run)
        .where(Run.workspace_id == workspace_id, Run.id == run_id)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def test_unknown_write_schedules_reconciliation(
    db_session: AsyncSession,
    operation_service: OperationService,
    seeded_write_tool_call: ToolCall,
    seeded_run: Run,
) -> None:
    """计划原文:UNKNOWN → Run WAITING_RECONCILIATION + RECONCILE_OPERATION Job。"""
    begun = await operation_service.begin(
        seeded_write_tool_call.id,
        adapter_key="test-adapter",
        expected_state=TEST_EXPECTED_STATE,
    )
    result = await operation_service.record_result(tool_result_unknown(begun.tool_call_id))
    assert result.run_status == RunStatus.WAITING_RECONCILIATION
    assert result.next_job_kind == RunJobKind.RECONCILE_OPERATION
    run = await fresh_run(db_session, seeded_run.workspace_id, seeded_run.id)
    assert run.status == PersistenceRunStatus.WAITING_RECONCILIATION


async def test_begin_is_idempotent_and_visible_after_short_transaction(
    engine: AsyncEngine,
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    leased_run,
    operation_service: OperationService,
) -> None:
    """begin 幂等:稳定 operation_id 不重新生成;短事务提交后独立连接可见。"""
    workspace_id = seeded_workspace.workspace.id
    tool_version = await db_session.get(ToolVersion, seeded_write_tool_call.tool_version_id)
    assert tool_version is not None
    cleanup_ids = {
        "workspace_id": workspace_id,
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
    # 种子(含领取的租约)先提交;begin 作为独立短事务提交,模拟执行前持久边界。
    await db_session.commit()
    try:
        first = await operation_service.begin(
            seeded_write_tool_call.id,
            adapter_key="test-adapter",
            expected_state=TEST_EXPECTED_STATE,
            supports_reconciliation=True,
        )
        await db_session.commit()
        second = await operation_service.begin(
            seeded_write_tool_call.id,
            adapter_key="test-adapter",
            expected_state=TEST_EXPECTED_STATE,
            supports_reconciliation=True,
        )
        await db_session.commit()

        assert first.operation_id == second.operation_id
        assert second.created is False

        verify = AsyncSession(engine, expire_on_commit=False)
        try:
            operation = (
                await verify.scalars(
                    select(Operation).where(
                        Operation.workspace_id == workspace_id,
                        Operation.operation_id == first.operation_id,
                    )
                )
            ).one()
            assert operation.status.value == "EXECUTING"
            assert operation.idempotency_key == first.idempotency_key
            attempts = (
                await verify.scalars(
                    select(OperationAttempt).where(
                        OperationAttempt.workspace_id == workspace_id,
                        OperationAttempt.operation_row_id == operation.id,
                    )
                )
            ).all()
            assert len(attempts) >= 1
            assert all(attempt.kind.value == "EXECUTE" for attempt in attempts)
            tool_call = (
                await verify.scalars(
                    select(ToolCall).where(
                        ToolCall.workspace_id == workspace_id,
                        ToolCall.id == seeded_write_tool_call.id,
                    )
                )
            ).one()
            assert tool_call.status == ToolCallStatus.EXECUTING
            assert tool_call.operation_id == first.operation_id
        finally:
            await verify.close()
    finally:
        await _cleanup_committed_seed(engine, cleanup_ids)


async def test_record_result_applied_completes_tool_call_and_keeps_run_running(
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    operation_service: OperationService,
    events_repository: EventRepository,
) -> None:
    """APPLIED:ToolCall SUCCEEDED、Operation APPLIED,Run 保持 RUNNING。"""
    begun = await operation_service.begin(
        seeded_write_tool_call.id,
        adapter_key="test-adapter",
        expected_state=TEST_EXPECTED_STATE,
    )
    result = await operation_service.record_result(
        OperationResultRecord(
            tool_call_id=begun.tool_call_id,
            outcome=OperationOutcome.APPLIED,
            payload={"result": "ok", "record_id": "R1"},
        )
    )
    assert result.operation_outcome == OperationOutcome.APPLIED
    assert result.tool_call_status == ToolCallStatus.SUCCEEDED
    assert result.run_status == RunStatus.RUNNING
    assert result.next_job_kind is None

    tool_call = await fresh_tool_call(
        db_session, seeded_run.workspace_id, seeded_write_tool_call.id
    )
    assert tool_call.status == ToolCallStatus.SUCCEEDED
    operation = await fresh_operation(db_session, seeded_run.workspace_id, begun.operation_id)
    assert operation.status.value == "APPLIED"
    run = await fresh_run(db_session, seeded_run.workspace_id, seeded_run.id)
    assert run.status == PersistenceRunStatus.RUNNING
    event_types = [event.event_type for event in await events_repository.list_for_run(
        workspace_id=seeded_run.workspace_id, run_id=seeded_run.id
    )]
    assert "TOOL_RESULT_RECORDED" in event_types


async def test_record_result_not_applied_returns_to_validation(
    db_session: AsyncSession,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    operation_service: OperationService,
) -> None:
    """NOT_APPLIED:ToolCall 回 VALIDATING(重走 PreToolUse),Operation NOT_APPLIED。"""
    begun = await operation_service.begin(
        seeded_write_tool_call.id,
        adapter_key="test-adapter",
        expected_state=TEST_EXPECTED_STATE,
    )
    result = await operation_service.record_result(
        OperationResultRecord(
            tool_call_id=begun.tool_call_id,
            outcome=OperationOutcome.NOT_APPLIED,
            payload={"result": "not_found"},
        )
    )
    assert result.tool_call_status == ToolCallStatus.VALIDATING
    tool_call = await fresh_tool_call(
        db_session, seeded_run.workspace_id, seeded_write_tool_call.id
    )
    assert tool_call.status == ToolCallStatus.VALIDATING
    run = await fresh_run(db_session, seeded_run.workspace_id, seeded_run.id)
    assert run.status == PersistenceRunStatus.RUNNING


async def test_stale_lease_token_rejected(
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    leased_run,
) -> None:
    """租约失效(伪造 token)→ 明确错误,状态不变(fencing)。"""
    service = OperationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=seeded_run.id,
        lease_token=uuid.uuid4(),
        expected_state_version=leased_run.state_version,
    )
    with pytest.raises(Exception, match="FENCING"):
        await service.begin(
            seeded_write_tool_call.id,
            adapter_key="test-adapter",
            expected_state=TEST_EXPECTED_STATE,
        )
    run = await fresh_run(db_session, seeded_run.workspace_id, seeded_run.id)
    assert run.status == PersistenceRunStatus.RUNNING
    assert run.state_version == leased_run.state_version


async def test_stale_state_version_rejected(
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    leased_run,
    operation_service: OperationService,
) -> None:
    """state_version CAS 失败 → InvalidTransition,Run 不迁移。"""
    from runtime.execution.transitions import InvalidTransition

    begun = await operation_service.begin(
        seeded_write_tool_call.id,
        adapter_key="test-adapter",
        expected_state=TEST_EXPECTED_STATE,
    )
    # 用过期版本构造第二个服务(模拟旧 Worker:version 已被并发推进)。
    stale_service = OperationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=seeded_run.id,
        lease_token=leased_run.lease_token,
        expected_state_version=leased_run.state_version + 100,
    )
    with pytest.raises(InvalidTransition):
        await stale_service.record_result(tool_result_unknown(begun.tool_call_id))
    run_after = await fresh_run(db_session, seeded_run.workspace_id, seeded_run.id)
    assert run_after.status == PersistenceRunStatus.RUNNING


async def test_record_result_without_begin_rejected(
    seeded_write_tool_call: ToolCall, operation_service: OperationService
) -> None:
    """未 begin(无 Operation)不得提交结果。"""
    with pytest.raises(OperationConflictError):
        await operation_service.record_result(
            OperationResultRecord(
                tool_call_id=seeded_write_tool_call.id,
                outcome=OperationOutcome.APPLIED,
                payload={"result": "ok"},
            )
        )


async def test_write_decision_writes_audit_events(
    db_session: AsyncSession, seeded_workspace, seeded_run: Run, seeded_write_tool_call: ToolCall
) -> None:
    """写操作 allowed/denied 各写 AuditEvent。"""
    await audit_write_decision(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=seeded_run.id,
        tool_call_id=seeded_write_tool_call.id,
        allowed=True,
    )
    await audit_write_decision(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=seeded_run.id,
        tool_call_id=seeded_write_tool_call.id,
        allowed=False,
        reason_code="POLICY_DENIED",
    )
    audit = AuditService(
        db_session, workspace_id=seeded_workspace.workspace.id, actor_id=seeded_workspace.user.id
    )
    actions = [event.action for event in await audit.list_for_run(
        workspace_id=seeded_workspace.workspace.id, run_id=seeded_run.id
    )]
    assert "TOOL_WRITE_ALLOWED" in actions
    assert "TOOL_WRITE_DENIED" in actions


async def _cleanup_committed_seed(engine: AsyncEngine, ids: dict) -> None:
    """按外键逆序清理已提交种子行(test_run_service 同构)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        ws = ids["workspace_id"]
        await session.execute(sa.delete(OperationAttempt).where(OperationAttempt.workspace_id == ws))
        await session.execute(sa.text("DELETE FROM agent_events WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM audit_events WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM run_jobs WHERE workspace_id = :ws"), {"ws": ws})
        await session.execute(sa.text("DELETE FROM run_attempts WHERE workspace_id = :ws"), {"ws": ws})
        # tool_calls 与 operations 相互外键引用:先解除 tool_calls.operation_id
        # 引用,再删 operations,最后删 tool_calls。
        await session.execute(
            sa.text("UPDATE tool_calls SET operation_id = NULL WHERE workspace_id = :ws"),
            {"ws": ws},
        )
        await session.execute(sa.delete(Operation).where(Operation.workspace_id == ws))
        await session.execute(
            sa.delete(ToolCall).where(ToolCall.workspace_id == ws)
        )
        await session.execute(
            sa.text("DELETE FROM messages WHERE workspace_id = :ws"), {"ws": ws}
        )
        await session.execute(
            sa.delete(ToolVersion).where(ToolVersion.id == ids["tool_version_id"])
        )
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
# ToolExecutor:两阶段执行编排(注入 fake handler,含响应丢失注入点)
# ---------------------------------------------------------------------------


class _FakeHandler:
    """可编排结果的 fake 写 Tool handler;``raise_error`` 模拟响应丢失。"""

    def __init__(self, *, status: str = "APPLIED", payload: dict | None = None,
                 raise_error: bool = False) -> None:
        self.status = status
        self.payload = payload if payload is not None else {"result": "ok"}
        self.raise_error = raise_error
        self.calls: list[dict] = []
        self.prepared_arguments: dict | None = None

    async def prepare(self, *, normalized_arguments: Mapping[str, object]) -> PreparedToolWrite:
        self.prepared_arguments = dict(normalized_arguments)
        return PreparedToolWrite(
            command={"kind": "fake"},
            expected_state=TEST_EXPECTED_STATE,
            request_fingerprint="fake-request-fingerprint",
        )

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        assert prepared.command == {"kind": "fake"}
        self.calls.append(
            {
                "tool_call_id": tool_call_id,
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
            }
        )
        if self.raise_error:
            raise ConnectionError("network dropped before response")
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id, status=self.status, payload=self.payload
        )


def _executor(
    db_session: AsyncSession, seeded_workspace, leased_run, handler: _FakeHandler
) -> ToolExecutor:
    return ToolExecutor(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=leased_run.run.id,
        lease_token=leased_run.lease_token,
        expected_state_version=leased_run.state_version,
        handler=handler,
        processor=ToolResultProcessor(
            output_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            }
        ),
        execution_profile=ToolExecutionProfile(adapter_key="test-adapter"),
    )


async def test_executor_applied_happy_path(
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    leased_run,
) -> None:
    """两阶段执行:begin → handler → record_result,APPLIED 全链路落库。"""
    handler = _FakeHandler(status="APPLIED", payload={"result": "ok"})
    executor = _executor(db_session, seeded_workspace, leased_run, handler)
    executed = await executor.execute(
        tool_call_id=seeded_write_tool_call.id, normalized_arguments={"listing_id": "L1"}
    )
    assert executed.recorded.operation_outcome == OperationOutcome.APPLIED
    assert executed.recorded.run_status == RunStatus.RUNNING
    assert len(handler.calls) == 1
    assert handler.calls[0]["operation_id"] == executed.begun.operation_id
    tool_call = await fresh_tool_call(
        db_session, seeded_run.workspace_id, seeded_write_tool_call.id
    )
    assert tool_call.status == ToolCallStatus.SUCCEEDED


async def test_executor_lost_response_yields_unknown_without_retry(
    db_session: AsyncSession,
    seeded_workspace,
    seeded_run: Run,
    seeded_write_tool_call: ToolCall,
    leased_run,
) -> None:
    """handler 抛异常(响应丢失)→ UNKNOWN:Run WAITING_RECONCILIATION,禁止重试。"""
    handler = _FakeHandler(raise_error=True)
    executor = _executor(db_session, seeded_workspace, leased_run, handler)
    executed = await executor.execute(
        tool_call_id=seeded_write_tool_call.id, normalized_arguments={"listing_id": "L1"}
    )
    assert executed.processed.operation_outcome == OperationOutcome.UNKNOWN
    assert executed.processed.tool_call_error == "RESPONSE_LOST"
    assert executed.processed.retry_allowed is False
    assert executed.recorded.run_status == RunStatus.WAITING_RECONCILIATION
    assert executed.recorded.next_job_kind == RunJobKind.RECONCILE_OPERATION
    tool_call = await fresh_tool_call(
        db_session, seeded_run.workspace_id, seeded_write_tool_call.id
    )
    assert tool_call.status == ToolCallStatus.UNKNOWN
