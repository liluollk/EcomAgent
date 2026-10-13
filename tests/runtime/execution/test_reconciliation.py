"""ReconciliationService 测试:回查语义与预算(计划 02 Task 8;架构文档 §4.1、§4.2)。

计划原文测试:
- ``test_not_applied_returns_to_validation``:回查 NOT_APPLIED 只能回到
  VALIDATING,绝不直接重执行(direct_execute is False)。
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.service import AuditService
from runtime.execution.operations import LeaseStaleError
from runtime.execution.reconciliation import ReconcileResult, ReconciliationService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    OperationStatus,
    ToolCallStatus,
)
from runtime.persistence.base import (
    RunStatus as PersistenceRunStatus,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall


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
