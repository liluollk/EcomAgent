"""OperationService:两阶段写的权威登记与结果提交(计划 02 Task 8;架构文档 §3.4、§4.2、§7、§8)。

权威语义(计划 Step 3 逐条):

- **两阶段写**:外部调用前的短事务创建或读取稳定 Operation(写 ``EXECUTING``
  与 EXECUTE Attempt;恢复、确认和重试不得重新生成 ``operation_id``),提交后
  才调用 Adapter;结果经 :meth:`OperationService.record_result` 在结果事务内
  提交(状态、脱敏结果与 AgentEvent 同事务);
- **结果分类**:适配器回执状态 → :class:`OperationOutcome`(APPLIED /
  NOT_APPLIED / UNKNOWN)确定性分类(:func:`classify_receipt_status`);
- **UNKNOWN 只能进 Reconciliation**:Run ``RUNNING → WAITING_RECONCILIATION``,
  返回 ``next_job_kind=RECONCILE_OPERATION`` 供编排层调度;禁止盲目重试;
- **NOT_APPLIED 只能回 VALIDATING**:ToolCall ``EXECUTING → VALIDATING``,
  必须重新通过完整 PreToolUse 后才可再次执行;
- **fencing 提交**:结果事务要求 ``job.lease_token == worker_lease_token`` 且
  ``run.state_version == expected_state_version``;token 无效/过期抛
  :class:`LeaseStaleError`,版本过期经 RunService CAS 抛
  :class:`~runtime.execution.transitions.InvalidTransition`;
- **审计**:写操作 allowed / denied / executed / reconciled / UNKNOWN 各写
  AuditEvent(幂等键、回执摘要等结构性元数据,不含参数值)。

事务边界:所有写只 flush 不 commit,提交/回滚归调用方(UnitOfWork)持有。
本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.service import AuditService
from runtime.events.writer import AgentEventWriter, AppendedEvent
from runtime.execution.run_service import RunService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    OperationAttemptKind,
    OperationAttemptStatus,
    OperationStatus,
    RunJobKind,
    ToolCallStatus,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, RunJob, ToolCall
from runtime.persistence.repositories.runs import RunRepository


# ---- 审计动作 ----

TOOL_WRITE_ALLOWED: Final = "TOOL_WRITE_ALLOWED"
TOOL_WRITE_DENIED: Final = "TOOL_WRITE_DENIED"
TOOL_WRITE_EXECUTED: Final = "TOOL_WRITE_EXECUTED"
TOOL_WRITE_RECONCILED: Final = "TOOL_WRITE_RECONCILED"
TOOL_WRITE_UNKNOWN: Final = "TOOL_WRITE_UNKNOWN"

# ---- 事件种类 ----

EVENT_TOOL_RESULT_RECORDED: Final = "TOOL_RESULT_RECORDED"
EVENT_RUN_WAITING_RECONCILIATION: Final = "RUN_WAITING_RECONCILIATION"

# ---- 结果分类 ----


class OperationOutcome(str, enum.Enum):
    """外部写入的确定性结果分类(架构文档 §7:APPLIED | NOT_APPLIED | UNKNOWN)。"""

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


_APPLIED_RECEIPT_STATUSES: Final[frozenset[str]] = frozenset({"APPLIED", "SUCCEEDED", "OK"})
_NOT_APPLIED_RECEIPT_STATUSES: Final[frozenset[str]] = frozenset(
    {"NOT_APPLIED", "FAILED", "REJECTED", "ERROR"}
)


def classify_receipt_status(status: str) -> OperationOutcome:
    """适配器回执状态 → OperationOutcome 的确定性分类。

    明确成功/失败映射到 APPLIED/NOT_APPLIED;其余(UNKNOWN、超时、无法识别)
    一律 UNKNOWN——不确定性只能交给回查,不允许猜测成功或失败。
    """
    normalized = status.strip().upper()
    if normalized in _APPLIED_RECEIPT_STATUSES:
        return OperationOutcome.APPLIED
    if normalized in _NOT_APPLIED_RECEIPT_STATUSES:
        return OperationOutcome.NOT_APPLIED
    return OperationOutcome.UNKNOWN


# ---- 错误 ----


class OperationConflictError(RuntimeError):
    """ToolCall/Operation 状态竞争或顺序错误(如未 begin 先 record_result)。"""


class LeaseStaleError(RuntimeError):
    """fencing 失败:lease token 与活动租约不匹配或租约已过期。"""


# ---- 值对象 ----


@dataclass(frozen=True)
class OperationResultRecord:
    """提交给 ``record_result`` 的结果记录(来自 ToolResultProcessor 或测试)。

    ``payload`` 是权威结果(处理器已完成密钥类脱敏;可为 None,如 UNKNOWN);
    ``error_code`` 携带 ``INVALID_TOOL_RESULT`` / ``RESPONSE_LOST`` 等诊断码;
    ``retry_allowed`` 为 True 仅表示"允许在重新通过完整 PreToolUse 后再次执行"。
    """

    tool_call_id: uuid.UUID
    outcome: OperationOutcome
    payload: Mapping[str, Any] | None = None
    error_code: str | None = None
    external_reference: str | None = None
    retry_allowed: bool = False


@dataclass(frozen=True)
class BegunOperation:
    """``begin`` 的结果;``created`` 为 False 表示复用了已存在的稳定 Operation。"""

    tool_call_id: uuid.UUID
    operation_id: uuid.UUID
    operation_row_id: uuid.UUID
    idempotency_key: str
    adapter_key: str
    status: OperationStatus
    attempt_no: int
    created: bool


@dataclass(frozen=True)
class RecordResultOutcome:
    """``record_result`` 的结果;Run 状态与下一步 Job 指令一并返回。"""

    tool_call_id: uuid.UUID
    operation_id: uuid.UUID
    operation_outcome: OperationOutcome
    tool_call_status: ToolCallStatus
    run_status: RunStatus
    next_job_kind: RunJobKind | None
    event: AppendedEvent


# ---- fencing 与审计辅助 ----


async def ensure_write_fencing(
    session: AsyncSession, *, workspace_id: uuid.UUID, run_id: uuid.UUID, lease_token: uuid.UUID
) -> RunJob:
    """校验 run 上存在持有该 token 的活动(LEASED)租约,返回该 Job。

    架构文档 §8:状态提交必须同时满足 ``job.lease_token == worker_lease_token``
    与 ``run.state_version == expected_state_version``;后者由 RunService 的 CAS
    保证。token 不匹配或租约已过期即抛 :class:`LeaseStaleError`。
    """
    stmt = select(RunJob).where(
        RunJob.workspace_id == workspace_id,
        RunJob.run_id == run_id,
        RunJob.status == "LEASED",
        RunJob.lease_token == lease_token,
    )
    job = (await session.scalars(stmt)).one_or_none()
    if job is None:
        raise LeaseStaleError(
            f"FENCING_TOKEN_INVALID: no active lease with token {lease_token} "
            f"on run {run_id} in workspace {workspace_id}"
        )
    if job.lease_expires_at is not None and job.lease_expires_at <= datetime.now(UTC):
        raise LeaseStaleError(
            f"FENCING_LEASE_EXPIRED: lease {job.id} on run {run_id} expired "
            f"at {job.lease_expires_at.isoformat()}"
        )
    return job


async def audit_write_decision(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_call_id: uuid.UUID,
    allowed: bool,
    reason_code: str | None = None,
) -> None:
    """写操作被允许/拒绝时写只追加审计(PreToolUse 决策落库的统一入口)。"""
    audit = AuditService(session, workspace_id=workspace_id, actor_id=actor_id)
    await audit.append(
        TOOL_WRITE_ALLOWED if allowed else TOOL_WRITE_DENIED,
        payload={"tool_call_id": str(tool_call_id), "reason_code": reason_code},
        run_id=run_id,
        tool_call_id=tool_call_id,
        reason_code=reason_code,
    )


def _rowcount(result: Any) -> int:
    return cast("CursorResult[Any]", result).rowcount


# ---- 服务 ----


class OperationService:
    """单个 Run 的写操作登记与结果提交;在调用方事务内执行,只 flush 不 commit。

    构造即绑定一次执行边界:fencing 凭据(lease_token)与读取 Run 时的
    ``state_version``;结果事务的 Run CAS 以该版本提交,过期即失败。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        run_id: uuid.UUID,
        lease_token: uuid.UUID,
        expected_state_version: int,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._run_id = run_id
        self._lease_token = lease_token
        self._expected_state_version = expected_state_version
        self._runs = RunService(
            session=session, workspace_id=workspace_id
        )
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)
        self._audit = AuditService(session, workspace_id=workspace_id, actor_id=actor_id)

    @property
    def lease_token(self) -> uuid.UUID:
        """绑定的 fencing token(测试与编排层读取)。"""
        return self._lease_token

    async def begin(
        self,
        tool_call_id: uuid.UUID,
        *,
        adapter_key: str,
        supports_native_idempotency: bool = False,
        supports_reconciliation: bool = False,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> BegunOperation:
        """执行边界:幂等创建或读取稳定 Operation,写 EXECUTING 与 EXECUTE Attempt。

        - ToolCall 已分配 ``operation_id`` 时复用(恢复/确认/重试不重新生成),
          未分配时生成并写回 ToolCall;
        - Operation 行存在即复用(幂等),否则以该 operation_id 创建(状态
          EXECUTING);每次 begin 都对应一次即将发生的外部调用,新增 Attempt;
        - ToolCall ``VALIDATING → EXECUTING``(条件更新,竞争抛冲突);
        - 同时写 TOOL_WRITE_ALLOWED 审计(到达执行边界即已获准执行)。
        """
        await ensure_write_fencing(
            self._session,
            workspace_id=self._workspace_id,
            run_id=self._run_id,
            lease_token=self._lease_token,
        )
        tool_call = await self._load_tool_call(tool_call_id)
        assigned_id = tool_call.operation_id
        existing = (
            await self._load_operation_row(assigned_id) if assigned_id is not None else None
        )
        created = False
        if existing is not None:
            operation_row = existing
            # Operation.operation_id 列非空;复用时以行上的稳定值为准。
            operation_id: uuid.UUID = existing.operation_id
        else:
            operation_id = assigned_id if assigned_id is not None else uuid.uuid4()
            key = idempotency_key if idempotency_key is not None else f"op:{operation_id}"
            # 先建 Operation 行,再回填 tool_calls.operation_id(复合外键
            # fk_tool_calls_operation 引用 operations(workspace_id, operation_id),
            # 顺序颠倒会违反外键)。
            operation_row = Operation(
                workspace_id=self._workspace_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=key,
                adapter_key=adapter_key,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
                status=OperationStatus.EXECUTING,
            )
            self._session.add(operation_row)
            await self._session.flush()
            if assigned_id is None:
                await self._assign_operation_id(tool_call_id, operation_id)
            created = True
        await self._mark_tool_call_executing(tool_call_id)
        attempt_no = await self._next_attempt_no(operation_row.id, OperationAttemptKind.EXECUTE)
        attempt = OperationAttempt(
            workspace_id=self._workspace_id,
            operation_row_id=operation_row.id,
            attempt_no=attempt_no,
            kind=OperationAttemptKind.EXECUTE,
            status=OperationAttemptStatus.RUNNING,
            request_fingerprint=request_fingerprint,
            started_at=datetime.now(UTC),
        )
        self._session.add(attempt)
        await self._session.flush()
        await self._audit.append(
            TOOL_WRITE_ALLOWED,
            payload={
                "tool_call_id": str(tool_call_id),
                "operation_id": str(operation_id),
                "adapter_key": adapter_key,
            },
            run_id=self._run_id,
            tool_call_id=tool_call_id,
        )
        return BegunOperation(
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            operation_row_id=operation_row.id,
            idempotency_key=operation_row.idempotency_key,
            adapter_key=operation_row.adapter_key,
            status=OperationStatus.EXECUTING,
            attempt_no=attempt_no,
            created=created,
        )

    async def record_result(self, result: OperationResultRecord) -> RecordResultOutcome:
        """结果事务:分类落库 + ToolCall/Operation/Attempt 状态迁移 + Run 事件。

        - APPLIED → Operation APPLIED、ToolCall SUCCEEDED、Run 保持 RUNNING;
        - NOT_APPLIED → Operation NOT_APPLIED、ToolCall 回 VALIDATING
          (必须重走完整 PreToolUse,不得直接重执行);
        - UNKNOWN → Operation/ToolCall UNKNOWN,Run ``RUNNING →
          WAITING_RECONCILIATION``,``next_job_kind=RECONCILE_OPERATION``,
          禁止盲目重试;
        - fencing:lease token 与 state_version 任一失效即抛明确错误。
        """
        job = await ensure_write_fencing(
            self._session,
            workspace_id=self._workspace_id,
            run_id=self._run_id,
            lease_token=self._lease_token,
        )
        del job  # fencing 已验证;Job 完成由编排层在提交后执行
        tool_call = await self._load_tool_call(result.tool_call_id)
        if tool_call.operation_id is None:
            raise OperationConflictError(
                f"tool call {result.tool_call_id} has no operation; call begin() first"
            )
        operation_row = await self._load_operation_row(tool_call.operation_id)
        if operation_row is None:
            raise OperationConflictError(
                f"operation {tool_call.operation_id} not found for tool call {result.tool_call_id}"
            )

        tool_call_status = _TOOL_CALL_STATUS_BY_OUTCOME[result.outcome]
        attempt_status = _ATTEMPT_STATUS_BY_OUTCOME[result.outcome]
        moved = await self._transition_tool_call(
            result.tool_call_id,
            new_status=tool_call_status,
            expected_statuses=frozenset({ToolCallStatus.EXECUTING}),
            failure_code=result.error_code,
        )
        if not moved:
            raise OperationConflictError(
                f"tool call {result.tool_call_id} is not EXECUTING; "
                f"result for outcome {result.outcome.value} rejected"
            )
        await self._update_operation(
            operation_row.id,
            status=_OPERATION_STATUS_BY_OUTCOME[result.outcome],
            last_receipt={
                "outcome": result.outcome.value,
                "error_code": result.error_code,
                "external_reference": result.external_reference,
                "payload": dict(result.payload) if result.payload is not None else None,
            },
        )
        await self._finish_running_attempt(
            operation_row.id, kind=OperationAttemptKind.EXECUTE, status=attempt_status,
            failure_code=result.error_code,
        )

        if result.outcome is OperationOutcome.UNKNOWN:
            transition = await self._runs.transition_run(
                run_id=self._run_id,
                target_status=RunStatus.WAITING_RECONCILIATION,
                event_kind=EVENT_RUN_WAITING_RECONCILIATION,
                event_payload={
                    "tool_call_id": str(result.tool_call_id),
                    "operation_id": str(operation_row.operation_id),
                    "error_code": result.error_code,
                },
                expected_status=RunStatus.RUNNING,
                expected_version=self._expected_state_version,
            )
            run_status = transition.target_status
            await self._audit.append(
                TOOL_WRITE_UNKNOWN,
                payload={
                    "tool_call_id": str(result.tool_call_id),
                    "operation_id": str(operation_row.operation_id),
                },
                run_id=self._run_id,
                tool_call_id=result.tool_call_id,
            )
            next_job_kind: RunJobKind | None = RunJobKind.RECONCILE_OPERATION
            event = transition.event
        else:
            event = await self._writer.append(
                self._run_id,
                EVENT_TOOL_RESULT_RECORDED,
                {
                    "tool_call_id": str(result.tool_call_id),
                    "operation_id": str(operation_row.operation_id),
                    "outcome": result.outcome.value,
                    "error_code": result.error_code,
                },
                tool_call_id=result.tool_call_id,
            )
            run = await self._load_run_status()
            run_status = run
            await self._audit.append(
                TOOL_WRITE_EXECUTED,
                payload={
                    "tool_call_id": str(result.tool_call_id),
                    "operation_id": str(operation_row.operation_id),
                    "outcome": result.outcome.value,
                },
                run_id=self._run_id,
                tool_call_id=result.tool_call_id,
            )
            next_job_kind = None
        return RecordResultOutcome(
            tool_call_id=result.tool_call_id,
            operation_id=operation_row.operation_id,
            operation_outcome=result.outcome,
            tool_call_status=tool_call_status,
            run_status=run_status,
            next_job_kind=next_job_kind,
            event=event,
        )

    # ---- 内部 ----

    async def _load_tool_call(self, tool_call_id: uuid.UUID) -> ToolCall:
        stmt = (
            select(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id, ToolCall.id == tool_call_id
            )
            # 条件更新不走 ORM 同步,populate_existing 绕过身份映射读最新状态。
            .execution_options(populate_existing=True)
        )
        tool_call = (await self._session.scalars(stmt)).one_or_none()
        if tool_call is None:
            raise OperationConflictError(f"tool call {tool_call_id} not found")
        return tool_call

    async def _load_operation_row(self, operation_id: uuid.UUID) -> Operation | None:
        stmt = select(Operation).where(
            Operation.workspace_id == self._workspace_id,
            Operation.operation_id == operation_id,
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def _load_run_status(self) -> RunStatus:
        """读取当前 Run 状态(条件更新不经 ORM 同步,须 populate_existing 重读)。"""
        run = await RunRepository(self._session).get(self._workspace_id, self._run_id)
        if run is None:
            raise OperationConflictError(f"run {self._run_id} not found")
        return RunStatus(run.status.value)

    async def _assign_operation_id(self, tool_call_id: uuid.UUID, operation_id: uuid.UUID) -> None:
        stmt = (
            sa.update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == tool_call_id,
                ToolCall.operation_id.is_(None),
            )
            .values(operation_id=operation_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)

    async def _mark_tool_call_executing(self, tool_call_id: uuid.UUID) -> None:
        stmt = (
            sa.update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == tool_call_id,
                ToolCall.status.in_([ToolCallStatus.VALIDATING, ToolCallStatus.EXECUTING]),
            )
            .values(status=ToolCallStatus.EXECUTING)
            .execution_options(synchronize_session=False)
        )
        if _rowcount(await self._session.execute(stmt)) != 1:
            raise OperationConflictError(
                f"tool call {tool_call_id} is not VALIDATING/EXECUTING; cannot begin execution"
            )

    async def _transition_tool_call(
        self,
        tool_call_id: uuid.UUID,
        *,
        new_status: ToolCallStatus,
        expected_statuses: frozenset[ToolCallStatus],
        failure_code: str | None,
    ) -> bool:
        stmt = (
            sa.update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == tool_call_id,
                ToolCall.status.in_(sorted(expected_statuses, key=lambda s: s.value)),
            )
            .values(status=new_status, failure_code=failure_code)
            .execution_options(synchronize_session=False)
        )
        return _rowcount(await self._session.execute(stmt)) == 1

    async def _update_operation(
        self,
        operation_row_id: uuid.UUID,
        *,
        status: OperationStatus,
        last_receipt: Mapping[str, Any],
    ) -> None:
        stmt = (
            sa.update(Operation)
            .where(Operation.workspace_id == self._workspace_id, Operation.id == operation_row_id)
            .values(status=status, last_receipt=dict(last_receipt))
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)

    async def _next_attempt_no(
        self, operation_row_id: uuid.UUID, kind: OperationAttemptKind
    ) -> int:
        stmt = (
            select(sa.func.count())
            .select_from(OperationAttempt)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
                OperationAttempt.kind == kind,
            )
        )
        return int(await self._session.scalar(stmt) or 0) + 1

    async def _finish_running_attempt(
        self,
        operation_row_id: uuid.UUID,
        *,
        kind: OperationAttemptKind,
        status: OperationAttemptStatus,
        failure_code: str | None,
    ) -> None:
        stmt = (
            sa.update(OperationAttempt)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
                OperationAttempt.kind == kind,
                OperationAttempt.status == OperationAttemptStatus.RUNNING,
            )
            .values(status=status, finished_at=datetime.now(UTC), failure_code=failure_code)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)


_TOOL_CALL_STATUS_BY_OUTCOME: Final[dict[OperationOutcome, ToolCallStatus]] = {
    OperationOutcome.APPLIED: ToolCallStatus.SUCCEEDED,
    OperationOutcome.NOT_APPLIED: ToolCallStatus.VALIDATING,
    OperationOutcome.UNKNOWN: ToolCallStatus.UNKNOWN,
}
_OPERATION_STATUS_BY_OUTCOME: Final[dict[OperationOutcome, OperationStatus]] = {
    OperationOutcome.APPLIED: OperationStatus.APPLIED,
    OperationOutcome.NOT_APPLIED: OperationStatus.NOT_APPLIED,
    OperationOutcome.UNKNOWN: OperationStatus.UNKNOWN,
}
_ATTEMPT_STATUS_BY_OUTCOME: Final[dict[OperationOutcome, OperationAttemptStatus]] = {
    OperationOutcome.APPLIED: OperationAttemptStatus.SUCCEEDED,
    OperationOutcome.NOT_APPLIED: OperationAttemptStatus.FAILED,
    # UNKNOWN:外部写入命运未卜,Attempt 既非成功也非失败。
    OperationOutcome.UNKNOWN: OperationAttemptStatus.ABANDONED,
}
