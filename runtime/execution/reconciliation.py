"""ReconciliationService:RECONCILE_OPERATION 回查(计划 02 Task 8;架构文档 §4.1、§4.2、§7)。

权威语义(计划 Step 3 逐条):

- 按 Run 领取 ``RECONCILE_OPERATION`` Job(租约由 LeaseService 颁发),回查结果
  经 :meth:`ReconciliationService.apply_result` 提交,fencing 与结果事务一致:
  ``job.lease_token`` 校验 + Run ``state_version`` CAS;
- **APPLIED** → Operation APPLIED、ToolCall ``UNKNOWN → SUCCEEDED``、Run
  ``WAITING_RECONCILIATION → QUEUED``(返回 ``next_job_kind=ADVANCE_RUN`` 指令,
  编排层据此调度恢复 Job);
- **NOT_APPLIED** → Operation NOT_APPLIED;ToolCall 由**写入重试预算**裁决:该
  Operation 已持久化的 ``EXECUTE`` Attempt 数 < ``write_retry_budget`` 时回
  ``VALIDATING``(必须重走完整 PreToolUse),否则落 ``FAILED`` 终态、不再产生可
  重排入口;两种情况 Run 均回 QUEUED(``next_job_kind=ADVANCE_RUN``)让模型据
  失败结果收尾;``direct_execute`` 恒为 False——绝不直接重执行;
- **UNKNOWN(仍不确定)** → Operation/ToolCall 保持 UNKNOWN 作为权威事实;
  预算内 Run 保持 WAITING_RECONCILIATION 并再次返回 RECONCILE_OPERATION 指令;
  超过回查预算(注入值)Run ``FAILED``(``RECONCILE_BUDGET_EXCEEDED``),但
  ToolCall/Operation 保留 UNKNOWN;
- 每次回查写一条 RECONCILE OperationAttempt(次数计数依据)并写
  TOOL_WRITE_RECONCILED 审计;Run 迁移经 RunService 写同事务 AgentEvent。

事务边界:只 flush 不 commit;本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import OUTCOME_FAILURE
from runtime.audit.service import AuditService
from runtime.events.writer import AgentEventWriter, AppendedEvent
from runtime.execution.operations import (
    TOOL_WRITE_RECONCILED,
    LeaseStaleError,
    ensure_write_fencing,
)
from runtime.execution.run_service import RunService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    OperationAttemptKind,
    OperationAttemptStatus,
    OperationStatus,
    RunJobKind,
    ToolCallStatus,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall


RECONCILE_BUDGET_EXCEEDED: Final = "RECONCILE_BUDGET_EXCEEDED"
"""回查预算耗尽仍不确定时 Run 的 failure_code。"""

EVENT_OPERATION_RECONCILED: Final = "OPERATION_RECONCILED"
EVENT_RUN_FAILED: Final = "RUN_FAILED"
EVENT_RUN_RESUMED: Final = "RUN_RESUMED"


class ReconcileResult(str, enum.Enum):
    """回查结论;UNKNOWN 表示本次回查仍无法消除不确定性。"""

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


class OperationNotFoundError(RuntimeError):
    """(workspace, operation_id) 无匹配 Operation 行。"""


@dataclass(frozen=True)
class ReconcileOutcome:
    """``apply_result`` 的结果;``direct_execute`` 恒为 False(语义不变量)。"""

    operation_id: uuid.UUID
    tool_call_id: uuid.UUID
    result: ReconcileResult
    operation_status: OperationStatus
    tool_call_status: ToolCallStatus
    run_status: RunStatus
    direct_execute: bool
    next_job_kind: RunJobKind | None
    reconcile_count: int
    budget_exhausted: bool
    event: AppendedEvent


class ReconciliationService:
    """单个 UNKNOWN Operation 的回查服务;在调用方事务内执行,只 flush 不 commit。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        run_id: uuid.UUID,
        operation_id: uuid.UUID,
        lease_token: uuid.UUID,
        expected_state_version: int,
        reconcile_budget: int,
        write_retry_budget: int,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._run_id = run_id
        self._operation_id = operation_id
        self._lease_token = lease_token
        self._expected_state_version = expected_state_version
        self._reconcile_budget = reconcile_budget
        self._write_retry_budget = write_retry_budget
        self._runs = RunService(session=session, workspace_id=workspace_id)
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)
        self._audit = AuditService(session, workspace_id=workspace_id, actor_id=actor_id)

    async def apply_result(self, result: ReconcileResult) -> ReconcileOutcome:
        """提交一次回查结论;状态迁移、事件与审计在同一事务内完成。"""
        await ensure_write_fencing(
            self._session,
            workspace_id=self._workspace_id,
            run_id=self._run_id,
            lease_token=self._lease_token,
        )
        operation_row = await self._load_operation()
        tool_call = await self._load_tool_call(operation_row.tool_call_id)
        # 预算计数按 RECONCILE kind;行唯一性(uq_operation_attempts_operation_attempt
        # 覆盖全部 kind)则按该 Operation 的全局 max(attempt_no)+1,避免与
        # EXECUTE Attempt 撞号(计划 02 Task 9 修复:真实 execute→UNKNOWN→
        # reconcile 链路此前必然违反唯一约束)。
        reconcile_count = await self._count_reconcile_attempts(operation_row.id) + 1
        attempt_no = await self._max_operation_attempt_no(operation_row.id) + 1
        self._session.add(
            OperationAttempt(
                workspace_id=self._workspace_id,
                operation_row_id=operation_row.id,
                attempt_no=attempt_no,
                kind=OperationAttemptKind.RECONCILE,
                status=OperationAttemptStatus.SUCCEEDED,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        )
        await self._session.flush()

        if result is ReconcileResult.UNKNOWN and reconcile_count >= self._reconcile_budget:
            # 预算 N 允许 N 次回查;第 N 次仍 UNKNOWN 即无剩余名额,Run FAILED。
            return await self._fail_run_on_budget(
                operation_row=operation_row,
                tool_call_id=tool_call.id,
                reconcile_count=reconcile_count,
            )

        if result is ReconcileResult.APPLIED:
            operation_status = OperationStatus.APPLIED
            tool_call_status = ToolCallStatus.SUCCEEDED
        elif result is ReconcileResult.NOT_APPLIED:
            operation_status = OperationStatus.NOT_APPLIED
            # 写入重试预算按持久化的 EXECUTE Attempt 数裁决(恢复重入沿用同一预算):
            # 未耗尽才允许回 VALIDATING 重排,耗尽则落 FAILED 终态、不再重排。
            executed = await self._count_execute_attempts(operation_row.id)
            tool_call_status = (
                ToolCallStatus.VALIDATING
                if executed < self._write_retry_budget
                else ToolCallStatus.FAILED
            )
        else:
            operation_status = OperationStatus.UNKNOWN
            tool_call_status = ToolCallStatus.UNKNOWN
        await self._set_operation_status(operation_row.id, operation_status)
        if not await self._transition_tool_call(
            tool_call.id,
            new_status=tool_call_status,
            expected_statuses=frozenset({ToolCallStatus.UNKNOWN, ToolCallStatus.EXECUTING}),
        ):
            from runtime.execution.operations import OperationConflictError

            raise OperationConflictError(
                f"tool call {tool_call.id} is not UNKNOWN/EXECUTING; reconcile result rejected"
            )

        if tool_call_status is ToolCallStatus.UNKNOWN:
            # 预算内仍不确定:Run 保持 WAITING_RECONCILIATION,再次调度回查。
            event = await self._writer.append(
                self._run_id,
                EVENT_OPERATION_RECONCILED,
                {
                    "operation_id": str(operation_row.operation_id),
                    "tool_call_id": str(tool_call.id),
                    "result": result.value,
                    "reconcile_count": attempt_no,
                },
                tool_call_id=tool_call.id,
            )
            run_status = RunStatus.WAITING_RECONCILIATION
            next_job_kind: RunJobKind | None = RunJobKind.RECONCILE_OPERATION
        else:
            transition = await self._runs.transition_run(
                run_id=self._run_id,
                target_status=RunStatus.QUEUED,
                event_kind=EVENT_RUN_RESUMED,
                event_payload={
                    "operation_id": str(operation_row.operation_id),
                    "tool_call_id": str(tool_call.id),
                    "reconcile_result": result.value,
                },
                expected_status=RunStatus.WAITING_RECONCILIATION,
                expected_version=self._expected_state_version,
            )
            event = await self._writer.append(
                self._run_id,
                EVENT_OPERATION_RECONCILED,
                {
                    "operation_id": str(operation_row.operation_id),
                    "tool_call_id": str(tool_call.id),
                    "result": result.value,
                    "reconcile_count": attempt_no,
                },
                tool_call_id=tool_call.id,
            )
            run_status = transition.target_status
            next_job_kind = RunJobKind.ADVANCE_RUN
        await self._audit.append(
            TOOL_WRITE_RECONCILED,
            payload={
                "operation_id": str(operation_row.operation_id),
                "tool_call_id": str(tool_call.id),
                "result": result.value,
                "reconcile_count": attempt_no,
            },
            run_id=self._run_id,
            tool_call_id=tool_call.id,
        )
        return ReconcileOutcome(
            operation_id=operation_row.operation_id,
            tool_call_id=tool_call.id,
            result=result,
            operation_status=operation_status,
            tool_call_status=tool_call_status,
            run_status=run_status,
            # 不变量:任何回查结论都不允许绕过 PreToolUse 直接重执行。
            direct_execute=False,
            next_job_kind=next_job_kind,
            reconcile_count=attempt_no,
            budget_exhausted=False,
            event=event,
        )

    # ---- 内部 ----

    async def _fail_run_on_budget(
        self, *, operation_row: Operation, tool_call_id: uuid.UUID, reconcile_count: int
    ) -> ReconcileOutcome:
        """预算耗尽仍 UNKNOWN:Run FAILED;ToolCall/Operation 保留 UNKNOWN。"""
        transition = await self._runs.transition_run(
            run_id=self._run_id,
            target_status=RunStatus.FAILED,
            event_kind=EVENT_RUN_FAILED,
            event_payload={
                "operation_id": str(operation_row.operation_id),
                "tool_call_id": str(tool_call_id),
                "failure_code": RECONCILE_BUDGET_EXCEEDED,
                "reconcile_count": reconcile_count,
            },
            expected_status=RunStatus.WAITING_RECONCILIATION,
            expected_version=self._expected_state_version,
        )
        # RunService.transition_run 只推进状态;确定性失败码在此落列。
        failure_code_stmt = (
            update(Run)
            .where(Run.workspace_id == self._workspace_id, Run.id == self._run_id)
            .values(failure_code=RECONCILE_BUDGET_EXCEEDED)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(failure_code_stmt)
        await self._audit.append(
            TOOL_WRITE_RECONCILED,
            payload={
                "operation_id": str(operation_row.operation_id),
                "tool_call_id": str(tool_call_id),
                "result": ReconcileResult.UNKNOWN.value,
                "reconcile_count": reconcile_count,
            },
            run_id=self._run_id,
            tool_call_id=tool_call_id,
            outcome=OUTCOME_FAILURE,
            reason_code=RECONCILE_BUDGET_EXCEEDED,
        )
        return ReconcileOutcome(
            operation_id=operation_row.operation_id,
            tool_call_id=tool_call_id,
            result=ReconcileResult.UNKNOWN,
            operation_status=OperationStatus.UNKNOWN,
            tool_call_status=ToolCallStatus.UNKNOWN,
            run_status=transition.target_status,
            direct_execute=False,
            next_job_kind=None,
            reconcile_count=reconcile_count,
            budget_exhausted=True,
            event=transition.event,
        )

    async def _load_operation(self) -> Operation:
        stmt = select(Operation).where(
            Operation.workspace_id == self._workspace_id,
            Operation.operation_id == self._operation_id,
        )
        operation_row = (await self._session.scalars(stmt)).one_or_none()
        if operation_row is None:
            raise OperationNotFoundError(
                f"operation {self._operation_id} not found in workspace {self._workspace_id}"
            )
        return operation_row

    async def _load_tool_call(self, tool_call_id: uuid.UUID) -> ToolCall:
        stmt = select(ToolCall).where(
            ToolCall.workspace_id == self._workspace_id, ToolCall.id == tool_call_id
        )
        tool_call = (await self._session.scalars(stmt)).one_or_none()
        if tool_call is None:
            from runtime.execution.operations import OperationConflictError

            raise OperationConflictError(f"tool call {tool_call_id} not found")
        return tool_call

    async def _max_operation_attempt_no(self, operation_row_id: uuid.UUID) -> int:
        """该 Operation 全部 kind Attempt 的最大序号(行唯一性跨 kind)。"""
        stmt = (
            select(sa.func.max(OperationAttempt.attempt_no))
            .select_from(OperationAttempt)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
            )
        )
        current = await self._session.scalar(stmt)
        return 0 if current is None else int(current)

    async def _count_reconcile_attempts(self, operation_row_id: uuid.UUID) -> int:
        stmt = (
            select(sa.func.count())
            .select_from(OperationAttempt)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
                OperationAttempt.kind == OperationAttemptKind.RECONCILE,
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    async def _count_execute_attempts(self, operation_row_id: uuid.UUID) -> int:
        """该 Operation 已持久化的 EXECUTE 次数(写入重试预算的计数依据)。

        只读 ``OperationAttempt`` 事实,不从内存计数推断,使幂等恢复重入沿用
        同一预算。
        """
        stmt = (
            select(sa.func.count())
            .select_from(OperationAttempt)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
                OperationAttempt.kind == OperationAttemptKind.EXECUTE,
            )
        )
        return int(await self._session.scalar(stmt) or 0)

    async def _set_operation_status(
        self, operation_row_id: uuid.UUID, status: OperationStatus
    ) -> None:
        stmt = (
            update(Operation)
            .where(Operation.workspace_id == self._workspace_id, Operation.id == operation_row_id)
            .values(status=status)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)

    async def _transition_tool_call(
        self,
        tool_call_id: uuid.UUID,
        *,
        new_status: ToolCallStatus,
        expected_statuses: frozenset[ToolCallStatus],
    ) -> bool:
        stmt = (
            update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == tool_call_id,
                ToolCall.status.in_(sorted(expected_statuses, key=lambda s: s.value)),
            )
            .values(status=new_status)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return getattr(result, "rowcount", 0) == 1


__all__ = [
    "RECONCILE_BUDGET_EXCEEDED",
    "LeaseStaleError",
    "OperationNotFoundError",
    "ReconcileOutcome",
    "ReconcileResult",
    "ReconciliationService",
]
