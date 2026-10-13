"""ReconciliationService 测试:回查语义与预算(计划 02 Task 8;架构文档 §4.1、§4.2)。

计划原文测试:
- ``test_not_applied_returns_to_validation``:回查 NOT_APPLIED 只能回到
  VALIDATING,绝不直接重执行(direct_execute is False)。
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.service import AuditService
from runtime.execution.operations import LeaseStaleError, OperationConflictError
from runtime.execution.reconciliation import (
    WRITE_RETRY_BUDGET_EXCEEDED,
    ReconcileResult,
    ReconciliationService,
)
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    MessageRole,
    OperationAttemptKind,
    OperationAttemptStatus,
    OperationStatus,
    RunJobKind,
    ToolCallStatus,
)
from runtime.persistence.base import (
    RunStatus as PersistenceRunStatus,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall
from runtime.persistence.models.interaction import Message


async def _tool_messages(
    session: AsyncSession, *, session_id: uuid.UUID, tool_call_id: uuid.UUID
) -> list[Message]:
    """该 call_id 已落库的 TOOL 消息(按序号);用于验证配对与幂等。"""
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.role == MessageRole.TOOL,
            Message.structured_content["call_id"].astext == str(tool_call_id),
        )
        .order_by(Message.sequence_no)
    )
    return list((await session.scalars(stmt)).all())


async def fresh_tool_call(
    session: AsyncSession, workspace_id: uuid.UUID, tool_call_id: uuid.UUID
) -> ToolCall:
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


async def test_not_applied_returns_to_validation(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """计划原文:NOT_APPLIED → ToolCall VALIDATING,direct_execute 为 False。"""
    result = await reconciliation_service.apply_result(ReconcileResult.NOT_APPLIED)
    assert result.tool_call_status == ToolCallStatus.VALIDATING
    assert result.direct_execute is False

    tool_call = await fresh_tool_call(
        db_session, seeded_workspace.workspace.id, uncertain_write.tool_call.id
    )
    assert tool_call.status == ToolCallStatus.VALIDATING
    operation = await fresh_operation(
        db_session, seeded_workspace.workspace.id, uncertain_write.operation.operation_id
    )
    assert operation.status == OperationStatus.NOT_APPLIED
    run = await fresh_run(db_session, seeded_workspace.workspace.id, uncertain_write.run.id)
    assert run.status == PersistenceRunStatus.QUEUED
    assert result.next_job_kind is not None and result.next_job_kind.value == "ADVANCE_RUN"


async def test_applied_completes_tool_call_and_resumes_run(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """回查 APPLIED:ToolCall SUCCEEDED、Run 回 QUEUED 继续。"""
    result = await reconciliation_service.apply_result(ReconcileResult.APPLIED)
    assert result.tool_call_status == ToolCallStatus.SUCCEEDED
    assert result.run_status == RunStatus.QUEUED
    assert result.direct_execute is False
    operation = await fresh_operation(
        db_session, seeded_workspace.workspace.id, uncertain_write.operation.operation_id
    )
    assert operation.status == OperationStatus.APPLIED
    tool_call = await fresh_tool_call(
        db_session, seeded_workspace.workspace.id, uncertain_write.tool_call.id
    )
    assert tool_call.status == ToolCallStatus.SUCCEEDED


async def test_unknown_within_budget_reschedules_reconciliation(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """回查仍 UNKNOWN(预算内):Run 保持 WAITING_RECONCILIATION,再次调度回查。"""
    result = await reconciliation_service.apply_result(ReconcileResult.UNKNOWN)
    assert result.run_status == RunStatus.WAITING_RECONCILIATION
    assert result.next_job_kind is not None
    assert result.next_job_kind.value == "RECONCILE_OPERATION"
    assert result.reconcile_count == 1
    assert result.budget_exhausted is False

    operation = await fresh_operation(
        db_session, seeded_workspace.workspace.id, uncertain_write.operation.operation_id
    )
    assert operation.status == OperationStatus.UNKNOWN
    tool_call = await fresh_tool_call(
        db_session, seeded_workspace.workspace.id, uncertain_write.tool_call.id
    )
    assert tool_call.status == ToolCallStatus.UNKNOWN
    run = await fresh_run(db_session, seeded_workspace.workspace.id, uncertain_write.run.id)
    assert run.status == PersistenceRunStatus.WAITING_RECONCILIATION

    second = await reconciliation_service.apply_result(ReconcileResult.UNKNOWN)
    assert second.reconcile_count == 2
    assert second.budget_exhausted is False


async def test_unknown_over_budget_fails_run_but_keeps_unknown_facts(
    db_session: AsyncSession,
    seeded_workspace,
    uncertain_write,
) -> None:
    """回查预算耗尽仍 UNKNOWN:Run FAILED,ToolCall/Operation 保留 UNKNOWN 权威事实。"""
    service = ReconciliationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=uncertain_write.run.id,
        operation_id=uncertain_write.operation.operation_id,
        lease_token=uncertain_write.lease_token,
        expected_state_version=uncertain_write.state_version,
        reconcile_budget=1,
        write_retry_budget=1,
    )
    result = await service.apply_result(ReconcileResult.UNKNOWN)
    assert result.budget_exhausted is True
    assert result.run_status == RunStatus.FAILED
    assert result.next_job_kind is None

    run = await fresh_run(db_session, seeded_workspace.workspace.id, uncertain_write.run.id)
    assert run.status == PersistenceRunStatus.FAILED
    assert run.failure_code == "RECONCILE_BUDGET_EXCEEDED"
    tool_call = await fresh_tool_call(
        db_session, seeded_workspace.workspace.id, uncertain_write.tool_call.id
    )
    assert tool_call.status == ToolCallStatus.UNKNOWN
    operation = await fresh_operation(
        db_session, seeded_workspace.workspace.id, uncertain_write.operation.operation_id
    )
    assert operation.status == OperationStatus.UNKNOWN


async def test_stale_lease_token_rejected(
    db_session: AsyncSession,
    seeded_workspace,
    uncertain_write,
) -> None:
    """回查结果提交同样受 fencing:失效 token 拒绝,状态不变。"""
    service = ReconciliationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
        run_id=uncertain_write.run.id,
        operation_id=uncertain_write.operation.operation_id,
        lease_token=uuid.uuid4(),
        expected_state_version=uncertain_write.state_version,
        reconcile_budget=2,
        write_retry_budget=1,
    )
    with pytest.raises(LeaseStaleError):
        await service.apply_result(ReconcileResult.APPLIED)
    tool_call = await fresh_tool_call(
        db_session, seeded_workspace.workspace.id, uncertain_write.tool_call.id
    )
    assert tool_call.status == ToolCallStatus.UNKNOWN


async def test_reconcile_attempts_are_counted_and_audited(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """每次回查写 RECONCILE Attempt 与 TOOL_WRITE_RECONCILED 审计。"""
    await reconciliation_service.apply_result(ReconcileResult.APPLIED)
    attempts = (
        await db_session.scalars(
            select(OperationAttempt).where(
                OperationAttempt.workspace_id == seeded_workspace.workspace.id,
                OperationAttempt.operation_row_id == uncertain_write.operation.id,
                OperationAttempt.kind == "RECONCILE",
            )
        )
    ).all()
    assert len(attempts) == 1
    audit = AuditService(
        db_session, workspace_id=seeded_workspace.workspace.id, actor_id=seeded_workspace.user.id
    )
    actions = [event.action for event in await audit.list_for_run(
        workspace_id=seeded_workspace.workspace.id, run_id=uncertain_write.run.id
    )]
    assert "TOOL_WRITE_RECONCILED" in actions


async def test_not_applied_returns_to_validating_only_within_budget(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """该 Operation 尚无 EXECUTE Attempt(< 预算 1):NOT_APPLIED 仍回 VALIDATING。

    ``direct_execute`` 恒为 False——重排必须重走完整 PreToolUse。
    """
    result = await reconciliation_service.apply_result(ReconcileResult.NOT_APPLIED)
    assert result.tool_call_status is ToolCallStatus.VALIDATING
    assert result.operation_status is OperationStatus.NOT_APPLIED
    assert result.direct_execute is False
    assert result.next_job_kind is RunJobKind.ADVANCE_RUN

    run = await fresh_run(db_session, seeded_workspace.workspace.id, uncertain_write.run.id)
    assert run.status == PersistenceRunStatus.QUEUED


async def test_not_applied_fails_tool_call_when_write_budget_exhausted(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """已有 1 个 EXECUTE Attempt(>= 预算 1):NOT_APPLIED 落 ToolCall FAILED 终态。

    预算耗尽后不得再产生可重排的 ``VALIDATING``;Run 仍回 QUEUED 让模型据失败
    结果收尾(``next_job_kind=ADVANCE_RUN``)。
    """
    db_session.add(
        OperationAttempt(
            workspace_id=seeded_workspace.workspace.id,
            operation_row_id=uncertain_write.operation.id,
            attempt_no=1,
            kind=OperationAttemptKind.EXECUTE,
            status=OperationAttemptStatus.FAILED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    result = await reconciliation_service.apply_result(ReconcileResult.NOT_APPLIED)
    assert result.tool_call_status is ToolCallStatus.FAILED
    assert result.operation_status is OperationStatus.NOT_APPLIED
    assert result.next_job_kind is RunJobKind.ADVANCE_RUN

    tool_call = await fresh_tool_call(
        db_session, seeded_workspace.workspace.id, uncertain_write.tool_call.id
    )
    assert tool_call.status == ToolCallStatus.FAILED
    assert tool_call.failure_code == WRITE_RETRY_BUDGET_EXCEEDED
    run = await fresh_run(db_session, seeded_workspace.workspace.id, uncertain_write.run.id)
    assert run.status == PersistenceRunStatus.QUEUED


async def _add_execute_attempt(db_session: AsyncSession, workspace_id, seed) -> None:
    """为 Operation 追加一个已完成的 EXECUTE Attempt(写入重试预算的计数依据)。"""
    db_session.add(
        OperationAttempt(
            workspace_id=workspace_id,
            operation_row_id=seed.operation.id,
            attempt_no=1,
            kind=OperationAttemptKind.EXECUTE,
            status=OperationAttemptStatus.FAILED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )
    await db_session.flush()


async def test_applied_reconcile_persists_paired_tool_result(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """APPLIED 终态必须原子落一条与原 assistant tool call 配对的 TOOL 消息。

    否则下一轮真实模型会看到"有 tool call 无 tool result"的非法历史。
    """
    result = await reconciliation_service.apply_result(ReconcileResult.APPLIED)
    assert result.tool_call_status is ToolCallStatus.SUCCEEDED

    messages = await _tool_messages(
        db_session,
        session_id=uncertain_write.run.session_id,
        tool_call_id=uncertain_write.tool_call.id,
    )
    assert len(messages) == 1
    message = messages[0]
    assert message.role is MessageRole.TOOL
    assert message.structured_content is not None
    assert message.structured_content["call_id"] == str(uncertain_write.tool_call.id)
    # Tool 名的解析来自 ToolVersion → ToolDefinition,不是空占位。
    assert message.structured_content["tool_name"]
    payload = json.loads(message.content)
    assert payload["operation_outcome"] == "APPLIED"
    assert payload["error_code"] is None
    assert payload["reconcile_result"] == "APPLIED"


async def test_terminal_not_applied_persists_tool_result_with_failure_code(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """预算耗尽的 NOT_APPLIED:ToolResult 带稳定失败码 WRITE_RETRY_BUDGET_EXCEEDED。"""
    await _add_execute_attempt(db_session, seeded_workspace.workspace.id, uncertain_write)

    result = await reconciliation_service.apply_result(ReconcileResult.NOT_APPLIED)
    assert result.tool_call_status is ToolCallStatus.FAILED

    messages = await _tool_messages(
        db_session,
        session_id=uncertain_write.run.session_id,
        tool_call_id=uncertain_write.tool_call.id,
    )
    assert len(messages) == 1
    payload = json.loads(messages[0].content)
    assert payload["operation_outcome"] == "NOT_APPLIED"
    assert payload["error_code"] == WRITE_RETRY_BUDGET_EXCEEDED
    assert payload["reconcile_result"] == "NOT_APPLIED"


async def test_inflight_not_applied_writes_no_tool_result(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """预算未耗尽的 NOT_APPLIED 回 VALIDATING:仍在飞行,不得抢先写 ToolResult。"""
    result = await reconciliation_service.apply_result(ReconcileResult.NOT_APPLIED)
    assert result.tool_call_status is ToolCallStatus.VALIDATING

    assert (
        await _tool_messages(
            db_session,
            session_id=uncertain_write.run.session_id,
            tool_call_id=uncertain_write.tool_call.id,
        )
        == []
    )


async def test_unknown_within_budget_writes_no_tool_result(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """预算内仍 UNKNOWN:结果未定,不得写 ToolResult。"""
    result = await reconciliation_service.apply_result(ReconcileResult.UNKNOWN)
    assert result.tool_call_status is ToolCallStatus.UNKNOWN

    assert (
        await _tool_messages(
            db_session,
            session_id=uncertain_write.run.session_id,
            tool_call_id=uncertain_write.tool_call.id,
        )
        == []
    )


async def test_replaying_applied_reconcile_does_not_duplicate_tool_result(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """同一回查恢复重入两次:第二次被状态 CAS 拒绝,不重复生成 ToolResult。"""
    await reconciliation_service.apply_result(ReconcileResult.APPLIED)
    assert (
        len(
            await _tool_messages(
                db_session,
                session_id=uncertain_write.run.session_id,
                tool_call_id=uncertain_write.tool_call.id,
            )
        )
        == 1
    )

    # ToolCall 已 SUCCEEDED,再回查同一 Operation 属非法重入。
    with pytest.raises(OperationConflictError):
        await reconciliation_service.apply_result(ReconcileResult.APPLIED)

    assert (
        len(
            await _tool_messages(
                db_session,
                session_id=uncertain_write.run.session_id,
                tool_call_id=uncertain_write.tool_call.id,
            )
        )
        == 1
    )


async def test_reconcile_count_counts_reconcile_attempts_not_all_attempts(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """已有 EXECUTE Attempt(全局 attempt_no=1)时,首次回查的 reconcile_count 仍为 1。

    此前事件/返回值误用全局 ``attempt_no``,真实执行一次再回查会被记成 2。
    """
    await _add_execute_attempt(db_session, seeded_workspace.workspace.id, uncertain_write)

    result = await reconciliation_service.apply_result(ReconcileResult.APPLIED)
    assert result.reconcile_count == 1
    assert result.event.payload["reconcile_count"] == 1


async def test_reconcile_refreshes_last_receipt_and_keeps_execute_evidence(
    db_session: AsyncSession,
    reconciliation_service: ReconciliationService,
    seeded_workspace,
    uncertain_write,
) -> None:
    """回查刷新 last_receipt 为 RECONCILIATION,原 EXECUTE 证据仍在 Attempt 中。"""
    await _add_execute_attempt(db_session, seeded_workspace.workspace.id, uncertain_write)

    await reconciliation_service.apply_result(ReconcileResult.APPLIED)

    operation = await fresh_operation(
        db_session, seeded_workspace.workspace.id, uncertain_write.operation.operation_id
    )
    assert operation.last_receipt is not None
    assert operation.last_receipt["stage"] == "RECONCILIATION"
    assert operation.last_receipt["result"] == "APPLIED"
    execute_attempts = (
        await db_session.scalars(
            select(OperationAttempt).where(
                OperationAttempt.workspace_id == seeded_workspace.workspace.id,
                OperationAttempt.operation_row_id == uncertain_write.operation.id,
                OperationAttempt.kind == OperationAttemptKind.EXECUTE,
            )
        )
    ).all()
    assert len(execute_attempts) == 1
