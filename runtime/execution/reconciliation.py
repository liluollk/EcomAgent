"""ReconciliationService:不确定外部写的回查(计划 02 Task 8;架构文档 §4.1、§4.2、§7)。

权威语义(计划 Step 3 逐条):

- 由 RunExecutor 在恢复扫描或 ``next_step=RECONCILE`` 时调用;回查结果经
  :meth:`ReconciliationService.apply_result` 提交,与 Run ``state_version``
  CAS 同一事务;
- **APPLIED** → Operation APPLIED、ToolCall ``UNKNOWN → SUCCEEDED``、Run
  ``WAITING_RECONCILIATION → QUEUED``(返回 ``next_step=ADVANCE`` 指令,
  执行器据此继续推进同一 Run);
- **NOT_APPLIED** → Operation NOT_APPLIED;ToolCall 由**写入重试预算**裁决:该
  Operation 已持久化的 ``EXECUTE`` Attempt 数 < ``write_retry_budget`` 时回
  ``VALIDATING``(必须重走完整 PreToolUse),否则落 ``FAILED`` 终态并写
  ``failure_code=WRITE_RETRY_BUDGET_EXCEEDED``、不再产生可重排入口;两种情况 Run
  均回 QUEUED(``next_step=ADVANCE``)让模型据失败结果收尾;
  ``direct_execute`` 恒为 False——绝不直接重执行;
- **终态回填模型可见结果**:APPLIED 与终态 NOT_APPLIED 都必须在同一事务内落一条
  与原 assistant tool call 配对的 TOOL 消息(``call_id`` 即 ``ToolCall.id``)。
  否则下一轮真实模型会看到"有 tool call 却缺 tool result"的非法历史,Provider
  可能直接拒绝,模型也无法据结果收尾;重复恢复不得重复写该消息;
- **UNKNOWN(仍不确定)** → Operation/ToolCall 保持 UNKNOWN 作为权威事实;
  预算内 Run 保持 WAITING_RECONCILIATION 并再次返回 ``next_step=RECONCILE``;
  超过回查预算(注入值)Run ``FAILED``(``RECONCILE_BUDGET_EXCEEDED``),但
  ToolCall/Operation 保留 UNKNOWN;
- 每次回查写一条 RECONCILE OperationAttempt(次数计数依据)、刷新
  ``Operation.last_receipt``(``stage=RECONCILIATION``;原 EXECUTION 证据由
  ``EXECUTE`` Attempt 与追加式 Event/审计保留)并写 TOOL_WRITE_RECONCILED 审计;
  Run 迁移经 RunService 写同事务 AgentEvent。事件/审计里的 ``reconcile_count``
  只统计 RECONCILE,与 ``attempt_no``(跨 kind 的行序号)不是同一口径。

事务边界:只 flush 不 commit;本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import OUTCOME_FAILURE
from runtime.audit.service import AuditService
from runtime.events.writer import AgentEventWriter, AppendedEvent
from runtime.execution.operations import (
    RECEIPT_STAGE_RECONCILIATION,
    TOOL_WRITE_RECONCILED,
    OperationOutcome,
)
from runtime.execution.run_service import RunService
from runtime.execution.transitions import InvalidTransition
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    MessageRole,
    MessageStatus,
    OperationAttemptKind,
    OperationAttemptStatus,
    OperationStatus,
    RunNextStep,
    ToolCallStatus,
)
from runtime.persistence.models.configuration import ToolDefinition, ToolVersion
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall
from runtime.persistence.models.interaction import Message


RECONCILE_BUDGET_EXCEEDED: Final = "RECONCILE_BUDGET_EXCEEDED"
"""回查预算耗尽仍不确定时 Run 的 failure_code。"""

WRITE_RETRY_BUDGET_EXCEEDED: Final = "WRITE_RETRY_BUDGET_EXCEEDED"
"""写入重试预算耗尽后 NOT_APPLIED 的 ``ToolCall.failure_code``(稳定错误码)。

区别于 :data:`RECONCILE_BUDGET_EXCEEDED`:后者是回查次数用尽、写入结果仍未知
(Run FAILED);本码是回查已确认未生效、但该 Operation 的 ``EXECUTE`` 次数已达
上界,于是 ToolCall 落 FAILED 终态并把原因返回模型,不再重排队。
"""

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
    next_step: RunNextStep | None
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
        expected_state_version: int,
        reconcile_budget: int,
        write_retry_budget: int,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._run_id = run_id
        self._operation_id = operation_id
        self._expected_state_version = expected_state_version
        self._reconcile_budget = reconcile_budget
        self._write_retry_budget = write_retry_budget
        self._runs = RunService(session=session, workspace_id=workspace_id)
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)
        self._audit = AuditService(session, workspace_id=workspace_id, actor_id=actor_id)

    async def apply_result(self, result: ReconcileResult) -> ReconcileOutcome:
        """提交一次回查结论;状态迁移、事件与审计在同一事务内完成。"""
        operation_row = await self._load_operation()
        # 版本校验必须在任何写入之前:UNKNOWN 分支没有 Run 迁移,若把校验留到
        # 末尾的 CAS,过期版本的 UNKNOWN 结论会被静默接受;而 APPLIED/NOT_APPLIED
        # 的 CAS 失败时,已 flush 的 Operation/ToolCall 改动也会留在调用方会话里
        # (调用方若不整体回滚,就会出现"记为 APPLIED 但 Run 未恢复")。
        await self._ensure_state_version_matches()
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

        failure_code: str | None = None
        if result is ReconcileResult.APPLIED:
            operation_status = OperationStatus.APPLIED
            tool_call_status = ToolCallStatus.SUCCEEDED
        elif result is ReconcileResult.NOT_APPLIED:
            operation_status = OperationStatus.NOT_APPLIED
            # 写入重试预算按持久化的 EXECUTE Attempt 数裁决(恢复重入沿用同一预算):
            # 未耗尽才允许回 VALIDATING 重排,耗尽则落 FAILED 终态、不再重排,
            # 并把稳定错误码返回模型。
            executed = await self._count_execute_attempts(operation_row.id)
            if executed < self._write_retry_budget:
                tool_call_status = ToolCallStatus.VALIDATING
            else:
                tool_call_status = ToolCallStatus.FAILED
                failure_code = WRITE_RETRY_BUDGET_EXCEEDED
        else:
            operation_status = OperationStatus.UNKNOWN
            tool_call_status = ToolCallStatus.UNKNOWN
        await self._set_operation_status(operation_row.id, operation_status)
        # 回查刷新最新回执,使 last_receipt 的来源标记与当前状态一致;原 EXECUTION
        # 证据由 EXECUTE Attempt 与追加式 Event/审计保留,不依赖会被覆盖的 last_receipt。
        await self._update_operation_receipt(
            operation_row.id,
            result=result,
            operation_status=operation_status,
            tool_call_status=tool_call_status,
            failure_code=failure_code,
        )
        if not await self._transition_tool_call(
            tool_call.id,
            new_status=tool_call_status,
            expected_statuses=frozenset({ToolCallStatus.UNKNOWN, ToolCallStatus.EXECUTING}),
            failure_code=failure_code,
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
                    "reconcile_count": reconcile_count,
                },
                tool_call_id=tool_call.id,
            )
            run_status = RunStatus.WAITING_RECONCILIATION
            next_step: RunNextStep | None = RunNextStep.RECONCILE
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
                    "reconcile_count": reconcile_count,
                },
                tool_call_id=tool_call.id,
            )
            run_status = transition.target_status
            next_step = RunNextStep.ADVANCE
            if tool_call_status in (ToolCallStatus.SUCCEEDED, ToolCallStatus.FAILED):
                # 终态才回填模型可见结果:VALIDATING 仍在飞行中,重执行由
                # AgentLoop 的正常写路径补 tool result,这里不得抢先写。
                await self._persist_terminal_tool_result(
                    tool_call=tool_call,
                    operation_id=operation_row.operation_id,
                    result=result,
                    tool_call_status=tool_call_status,
                    failure_code=failure_code,
                )
        await self._audit.append(
            TOOL_WRITE_RECONCILED,
            payload={
                "operation_id": str(operation_row.operation_id),
                "tool_call_id": str(tool_call.id),
                "result": result.value,
                "reconcile_count": reconcile_count,
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
            next_step=next_step,
            reconcile_count=reconcile_count,
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
        await self._update_operation_receipt(
            operation_row.id,
            result=ReconcileResult.UNKNOWN,
            operation_status=OperationStatus.UNKNOWN,
            tool_call_status=ToolCallStatus.UNKNOWN,
            failure_code=RECONCILE_BUDGET_EXCEEDED,
        )
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
            next_step=None,
            reconcile_count=reconcile_count,
            budget_exhausted=True,
            event=transition.event,
        )

    async def _ensure_state_version_matches(self) -> None:
        """回查结论的 stale 检查:版本不符则整笔拒绝,不改动任何行。"""
        run = (
            await self._session.scalars(
                select(Run)
                .where(Run.workspace_id == self._workspace_id, Run.id == self._run_id)
                .execution_options(populate_existing=True)
            )
        ).one_or_none()
        if run is None:
            raise LookupError(
                f"run {self._run_id} not found in workspace {self._workspace_id}"
            )
        if run.state_version != self._expected_state_version:
            raise InvalidTransition(
                current=RunStatus(run.status.value),
                target=RunStatus.QUEUED,
                reason=(
                    "stale reconcile result: state_version "
                    f"{self._expected_state_version} != {run.state_version}"
                ),
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
        failure_code: str | None,
    ) -> bool:
        """CAS 迁移 ToolCall;``failure_code`` 非空才改写。

        只有预算耗尽的终态失败需要写入稳定错误码;APPLIED / 预算内重排不改写,
        让"UNKNOWN 期间的失败码"作为恢复前的历史痕迹保留(状态本身已表达结果)。
        """
        values: dict[str, object] = {"status": new_status}
        if failure_code is not None:
            values["failure_code"] = failure_code
        stmt = (
            update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == tool_call_id,
                ToolCall.status.in_(sorted(expected_statuses, key=lambda s: s.value)),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        return getattr(result, "rowcount", 0) == 1

    async def _update_operation_receipt(
        self,
        operation_row_id: uuid.UUID,
        *,
        result: ReconcileResult,
        operation_status: OperationStatus,
        tool_call_status: ToolCallStatus,
        failure_code: str | None,
    ) -> None:
        """刷新 ``Operation.last_receipt`` 为与当前状态一致的回查回执。

        ``last_receipt`` 是"最新诊断快照"(会被后续结果覆盖),故这里写
        ``stage=RECONCILIATION``;原始 EXECUTION 证据由 ``EXECUTE`` Attempt 与
        追加式 AgentEvent / 审计事件保留,不依赖本列。
        """
        receipt: dict[str, Any] = {
            "stage": RECEIPT_STAGE_RECONCILIATION,
            "result": result.value,
            "operation_status": operation_status.value,
            "tool_call_status": tool_call_status.value,
            "error_code": failure_code,
        }
        stmt = (
            update(Operation)
            .where(Operation.workspace_id == self._workspace_id, Operation.id == operation_row_id)
            .values(last_receipt=receipt)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)

    async def _persist_terminal_tool_result(
        self,
        *,
        tool_call: ToolCall,
        operation_id: uuid.UUID,
        result: ReconcileResult,
        tool_call_status: ToolCallStatus,
        failure_code: str | None,
    ) -> None:
        """把回查终态写成与原 assistant tool call 配对的 TOOL 消息。

        下一轮真实模型据此收尾;缺该消息会让 Provider 见到"有 tool call 无
        tool result"的非法历史。幂等:同一 ``call_id`` 已存在 TOOL 消息则跳过,
        使崩溃恢复重入不会重复落库(回查结果事务本身也只 flush,由调用方提交)。
        """
        call_id = str(tool_call.id)
        exists = await self._session.scalar(
            select(sa.func.count())
            .select_from(Message)
            .where(
                Message.workspace_id == self._workspace_id,
                Message.role == MessageRole.TOOL,
                Message.structured_content["call_id"].astext == call_id,
            )
        )
        if exists:
            return
        session_id = await self._session.scalar(
            select(Run.session_id).where(
                Run.workspace_id == self._workspace_id, Run.id == self._run_id
            )
        )
        if session_id is None:
            raise OperationNotFoundError(
                f"run {self._run_id} not found while writing reconcile tool result"
            )
        outcome = (
            OperationOutcome.APPLIED
            if tool_call_status is ToolCallStatus.SUCCEEDED
            else OperationOutcome.NOT_APPLIED
        )
        message = Message(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=self._run_id,
            role=MessageRole.TOOL,
            status=MessageStatus.COMPLETED,
            content=_reconciliation_tool_content(
                operation_id=operation_id,
                result=result,
                outcome=outcome,
                failure_code=failure_code,
            ),
            structured_content={
                "call_id": call_id,
                "tool_name": await self._tool_name_for(tool_call.tool_version_id),
            },
            sequence_no=await self._next_message_sequence(session_id),
        )
        self._session.add(message)
        await self._session.flush()

    async def _next_message_sequence(self, session_id: uuid.UUID) -> int:
        """会话内消息序号:max+1(``uq_messages_session_sequence``)。"""
        current = await self._session.scalar(
            select(sa.func.max(Message.sequence_no)).where(
                Message.session_id == session_id
            )
        )
        return 1 if current is None else int(current) + 1

    async def _tool_name_for(self, tool_version_id: uuid.UUID) -> str | None:
        """由 ``ToolCall.tool_version_id`` 反查 Tool 的 stable key(供 tool message)。"""
        stmt = (
            select(ToolDefinition.stable_key)
            .select_from(ToolVersion)
            .join(ToolDefinition, ToolDefinition.id == ToolVersion.tool_id)
            .where(ToolVersion.id == tool_version_id)
        )
        return await self._session.scalar(stmt)


def _reconciliation_tool_content(
    *,
    operation_id: uuid.UUID,
    result: ReconcileResult,
    outcome: OperationOutcome,
    failure_code: str | None,
) -> str:
    """模型可见的回查终态结果(与 ToolResult 投影同构的最小 JSON)。

    固定带上 ``operation_outcome`` 与 ``error_code``:模型必须能读出"写入最终
    生效/未生效"以及未生效的原因,才能据此收尾或重新读取资源。
    """
    if outcome is OperationOutcome.APPLIED:
        text = "write confirmed applied by reconciliation"
    else:
        text = (
            "write confirmed not applied and the write-retry budget is exhausted; "
            "re-read the resource before deciding the next step"
        )
    return json.dumps(
        {
            "operation_outcome": outcome.value,
            "error_code": failure_code,
            "reconcile_result": result.value,
            "operation_id": str(operation_id),
            "text": text,
        },
        ensure_ascii=False,
    )


__all__ = [
    "RECONCILE_BUDGET_EXCEEDED",
    "WRITE_RETRY_BUDGET_EXCEEDED",
    "OperationNotFoundError",
    "ReconcileOutcome",
    "ReconcileResult",
    "ReconciliationService",
]
