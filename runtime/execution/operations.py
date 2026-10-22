"""OperationService:两阶段写的权威登记与结果提交(计划 02 Task 8;架构文档 §3.4、§4.2、§7、§8)。

权威语义(计划 Step 3 逐条):

- **两阶段写**:外部调用前的短事务创建或读取稳定 Operation(写 ``EXECUTING``
  与 EXECUTE Attempt;恢复、确认和重试不得重新生成 ``operation_id``),提交后
  才调用 Adapter;结果经 :meth:`OperationService.record_result` 在结果事务内
  提交(状态、脱敏结果与 AgentEvent 同事务);
- **begin 状态机**:可复用的 Operation 只能是 PREPARED(确认前只预约了
  ``operation_id``)、EXECUTING(同一 operation_id 的幂等重入)或 NOT_APPLIED
  (上次尝试确定未生效);已绑定目标时要求规范化 ``expected_state`` 与执行能力
  (adapter / 原生幂等 / 回查)完全一致;``expected_state IS NULL`` 只允许在
  PREPARED 或"带 ``DETERMINISTIC_REJECTION`` 标记且**不存在 EXECUTE Attempt**"
  的 NOT_APPLIED 下首次绑定目标(其余状态/来源一律 fail closed,历史脏数据不得
  被当成安全状态),且目标为空**不构成覆盖执行能力**的理由;进入执行边界即把状态
  推进 EXECUTING——这是恢复方识别"可能已执行"的唯一判据;
- **本地确定性拒绝**:prepare 阶段发现的确定性冲突走
  :meth:`OperationService.record_rejected_preparation`,直接落 NOT_APPLIED,
  不写 EXECUTING、不写 TOOL_WRITE_ALLOWED、不新增 EXECUTE Attempt,因而不产生
  外部执行的不确定窗口;拒绝一律不可重试(ToolCall 直接 ``FAILED``)。拒绝上下文
  只进 ``last_receipt``(最新诊断快照,会被后续结果覆盖),``request_fingerprint``
  另写只追加审计事件。**已发生过外部执行的 NOT_APPLIED 不被后续拒绝改写来源标记**:
  它保留原执行回执与完整 ``expected_state``,新拒绝只追加审计事件——否则一条确实
  调用过外部平台的 Operation 会被抹成"从未进入执行边界";
- **结果分类**:适配器回执状态 → :class:`OperationOutcome`(APPLIED /
  NOT_APPLIED / UNKNOWN)确定性分类(:func:`classify_receipt_status`);
- **UNKNOWN 只能进 Reconciliation**:Run ``RUNNING → WAITING_RECONCILIATION``,
  返回 ``next_step=RECONCILE`` 供执行器调度;禁止盲目重试;
- **NOT_APPLIED 的终态由 ``retry_allowed`` 决定**:显式可重试时 ToolCall
  ``EXECUTING → VALIDATING``(必须重新通过完整 PreToolUse 才可再次执行);
  否则 ToolCall 直接 ``FAILED``——"确定未生效"与"值得重试"是两件事,混为一谈
  会让编排层对同一个 ToolCall 无限重排队;
- **执行前状态校验**:写操作进入执行边界前要求 Run 仍为 ``RUNNING``
  (:func:`ensure_run_executable`);终态/挂起 Run 不得发起新的副作用。
  并发写入由 ``run.state_version == expected_state_version`` 的 CAS 裁决,
  版本过期抛 :class:`~runtime.execution.transitions.InvalidTransition`;
- **审计**:写操作 allowed / denied / executed / reconciled / UNKNOWN 各写
  AuditEvent(幂等键、回执摘要等结构性元数据,不含参数值)。

事务边界:所有写只 flush 不 commit,提交/回滚归调用方(UnitOfWork)持有。
本模块禁止 import adapters/agents/eval/apps。
"""

from __future__ import annotations

import enum
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import OUTCOME_FAILURE
from runtime.audit.service import AuditService
from runtime.events.writer import AgentEventWriter, AppendedEvent
from runtime.execution.run_service import RunService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    OperationAttemptKind,
    OperationAttemptStatus,
    OperationStatus,
    RunNextStep,
    ToolCallStatus,
)
from runtime.persistence.models.execution import Operation, OperationAttempt, Run, ToolCall
from runtime.persistence.repositories.runs import RunRepository


# ---- 审计动作 ----

TOOL_WRITE_ALLOWED: Final = "TOOL_WRITE_ALLOWED"
TOOL_WRITE_DENIED: Final = "TOOL_WRITE_DENIED"
TOOL_WRITE_REJECTED: Final = "TOOL_WRITE_REJECTED"
TOOL_WRITE_EXECUTED: Final = "TOOL_WRITE_EXECUTED"
TOOL_WRITE_RECONCILED: Final = "TOOL_WRITE_RECONCILED"
TOOL_WRITE_UNKNOWN: Final = "TOOL_WRITE_UNKNOWN"

# ---- 事件种类 ----

EVENT_TOOL_RESULT_RECORDED: Final = "TOOL_RESULT_RECORDED"
EVENT_RUN_WAITING_RECONCILIATION: Final = "RUN_WAITING_RECONCILIATION"

# ---- last_receipt 的受控来源标记 ----

RECEIPT_STAGE_PREPARATION: Final = "PREPARATION"
"""回执来源:prepare 阶段(尚未发起外部调用)。"""

RECEIPT_STAGE_EXECUTION: Final = "EXECUTION"
"""回执来源:外部执行边界。"""

RECEIPT_STAGE_RECONCILIATION: Final = "RECONCILIATION"
"""回执来源:Reconciliation 回查。

回查刷新 ``last_receipt`` 时写入该标记,使"最新诊断快照"的语义与当前状态
一致;原 EXECUTION 证据不依赖 ``last_receipt``(它本来就会被覆盖),而由
``OperationAttempt(kind=EXECUTE)`` 与追加式 AgentEvent/审计事件保留。
"""

REJECTION_KIND_DETERMINISTIC: Final = "DETERMINISTIC_REJECTION"
"""本地确定性拒绝的受控标记;只有它允许同一 Operation 在目标为空时重新绑定。"""

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


# ---- Operation 状态机(执行边界前提) ----

_BEGINNABLE_OPERATION_STATUSES: Final[frozenset[OperationStatus]] = frozenset(
    {
        # 确认前只预约了 operation_id,尚未绑定业务目标。
        OperationStatus.PREPARED,
        # 同一 operation_id 的幂等重入:崩溃恢复或 NOT_APPLIED 后的再次执行。
        OperationStatus.EXECUTING,
        # 上次尝试确定未生效(适配器判定或本地 prepare 拒绝):可以重新绑定目标。
        OperationStatus.NOT_APPLIED,
    }
)

_EXECUTABLE_TOOL_CALL_STATUSES: Final[tuple[ToolCallStatus, ...]] = (
    # 执行边界的 ToolCall 前提:VALIDATING(新执行)或 EXECUTING(幂等重入)。
    ToolCallStatus.VALIDATING,
    ToolCallStatus.EXECUTING,
)

_REJECTION_RECORDABLE_STATUSES: Final[frozenset[OperationStatus]] = frozenset(
    # 拒绝只允许改写"确定未生效"的操作;EXECUTING/UNKNOWN(可能已执行)与
    # APPLIED(已生效)只能走 Reconciliation。
    {OperationStatus.PREPARED, OperationStatus.NOT_APPLIED}
)


def _target_binding_allowed(operation_row: Operation) -> bool:
    """``expected_state IS NULL`` 时能否首次绑定业务目标(仅看状态与来源标记)。

    只认两种来源:

    - PREPARED:确认前只预约了 ``operation_id``,从未发起外部调用;
    - NOT_APPLIED 且 ``last_receipt`` 带 ``DETERMINISTIC_REJECTION`` 标记:上一次
      是本地 prepare 拒绝,同样从未发起外部调用。

    仅凭"状态 + 空目标"推断安全会把历史脏数据(例如适配器判定 NOT_APPLIED 却
    丢失目标记录的行)当成可重新绑定的状态,因此其他来源一律 fail closed。

    标记本身不充分:它只是 prepare 拒绝写入的诊断快照。调用方还必须确认该
    Operation **不存在 EXECUTE Attempt**(见
    :meth:`OperationService._operation_has_execute_attempt`),否则一条确实到过
    执行边界的行会借标记重新绑定。
    """
    if operation_row.status is OperationStatus.PREPARED:
        return True
    if operation_row.status is not OperationStatus.NOT_APPLIED:
        return False
    receipt = operation_row.last_receipt
    if not isinstance(receipt, Mapping):
        return False
    return (
        receipt.get("stage") == RECEIPT_STAGE_PREPARATION
        and receipt.get("kind") == REJECTION_KIND_DETERMINISTIC
    )


# ---- 值对象 ----


@dataclass(frozen=True)
class OperationResultRecord:
    """提交给 ``record_result`` 的结果记录(来自 ToolResultProcessor 或测试)。

    ``payload`` 是权威结果(处理器已完成密钥类脱敏;可为 None,如 UNKNOWN);
    ``error_code`` 携带 ``INVALID_TOOL_RESULT`` / ``RESPONSE_LOST`` 等诊断码;
    ``retry_allowed`` 是显式回执契约:为 True 仅表示"允许在重新通过完整
    PreToolUse 后再次执行",默认 False——``NOT_APPLIED`` 本身不蕴含可重试。
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
    retry_allowed: bool
    run_status: RunStatus
    next_step: RunNextStep | None
    event: AppendedEvent


# ---- fencing 与审计辅助 ----


async def ensure_run_executable(
    session: AsyncSession, *, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> Run:
    """校验 Run 仍可发起新的外部写。

    取代旧模型的 fencing token 校验:首版只有一个执行器,不存在"租约已被
    别人接管"的场景,真正需要拒绝的是"Run 已经不在 RUNNING"——用户取消、
    已终态或已挂起时不得再发起新的副作用。并发写入仍由 ``state_version``
    CAS 保护(见 :meth:`OperationService.record_result` 的 ``transition_run``)。
    """
    run = (
        await session.scalars(
            select(Run)
            .where(Run.workspace_id == workspace_id, Run.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if run is None:
        raise OperationConflictError(f"run {run_id} not found in workspace {workspace_id}")
    if run.status.value != RunStatus.RUNNING.value:
        raise OperationConflictError(
            f"run {run_id} is {run.status.value}; write execution requires RUNNING"
        )
    return run


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


def canonical_expected_state(value: Mapping[str, Any] | None) -> str:
    """``expected_state`` 的规范化 JSON;``None`` 归一为空对象。

    用于判断"复用同一 Operation 时目标是否被改写":键序、空白与非 JSON 值
    都不影响比较结果。
    """
    return json.dumps(
        {} if value is None else dict(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _require_same_execution_contract(
    operation_row: Operation,
    *,
    adapter_key: str,
    supports_native_idempotency: bool,
    supports_reconciliation: bool,
) -> None:
    """已绑定目标的 Operation 不得在执行能力上被改写。

    Adapter 与幂等/回查能力决定了"重复提交是否安全"与"结果能否被查证",
    因此必须与首次落库值完全一致;不一致即 fail closed,而不是以新值覆盖。
    """
    mismatches: list[str] = []
    if operation_row.adapter_key != adapter_key:
        mismatches.append(
            f"adapter_key persisted={operation_row.adapter_key!r} requested={adapter_key!r}"
        )
    if operation_row.supports_native_idempotency != supports_native_idempotency:
        mismatches.append(
            "supports_native_idempotency "
            f"persisted={operation_row.supports_native_idempotency} "
            f"requested={supports_native_idempotency}"
        )
    if operation_row.supports_reconciliation != supports_reconciliation:
        mismatches.append(
            "supports_reconciliation "
            f"persisted={operation_row.supports_reconciliation} "
            f"requested={supports_reconciliation}"
        )
    if mismatches:
        raise OperationConflictError(
            f"operation {operation_row.operation_id} 执行能力不一致:"
            + "; ".join(mismatches)
        )


# ---- 服务 ----


class OperationService:
    """单个 Run 的写操作登记与结果提交;在调用方事务内执行,只 flush 不 commit。

    构造即绑定一次执行边界:读取 Run 时的 ``state_version``;结果事务的
    Run CAS 以该版本提交,过期即失败(这是首版唯一的并发写保护)。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        run_id: uuid.UUID,
        expected_state_version: int,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._run_id = run_id
        self._expected_state_version = expected_state_version
        self._runs = RunService(
            session=session, workspace_id=workspace_id
        )
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)
        self._audit = AuditService(session, workspace_id=workspace_id, actor_id=actor_id)

    async def begin(
        self,
        tool_call_id: uuid.UUID,
        *,
        adapter_key: str,
        expected_state: Mapping[str, Any],
        supports_native_idempotency: bool = False,
        supports_reconciliation: bool = False,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> BegunOperation:
        """执行边界:幂等创建或读取稳定 Operation,写 EXECUTING 与 EXECUTE Attempt。

        - ToolCall 已分配 ``operation_id`` 时复用(恢复/确认/重试不重新生成),
          未分配时生成并写回 ToolCall;
        - 复用已有 Operation 时先收敛状态机(见
          :meth:`_prepare_existing_for_execution`):状态必须可执行、业务目标与
          执行能力必须一致,``expected_state IS NULL`` 只能首次绑定;任一不满足
          即抛 :class:`OperationConflictError`(fail closed);
        - 新建 Operation 时一并持久化服务端 ``expected_state``——它必须早于任何
          外部副作用落库,回查只能依据该记录;
        - 任何即将发起外部调用的 begin 都会让 Operation 处于 EXECUTING:这是
          恢复/回收方判断"可能已执行"的唯一判据;
        - 每次 begin 都对应一次即将发生的外部调用,新增 Attempt;
        - ToolCall ``VALIDATING → EXECUTING``(条件更新,竞争抛冲突);
        - 同时写 TOOL_WRITE_ALLOWED 审计(到达执行边界即已获准执行)。
        """
        await ensure_run_executable(
            self._session,
            workspace_id=self._workspace_id,
            run_id=self._run_id,
        )
        tool_call = await self._load_tool_call(tool_call_id)
        # 只读前提检查:ToolCall 必须处于可执行状态;失败时不留任何状态改动。
        if tool_call.status not in _EXECUTABLE_TOOL_CALL_STATUSES:
            raise OperationConflictError(
                f"tool call {tool_call_id} is not VALIDATING/EXECUTING; cannot begin execution"
            )
        assigned_id = tool_call.operation_id
        existing = (
            await self._load_operation_row(assigned_id) if assigned_id is not None else None
        )
        created = False
        if existing is not None:
            operation_row = existing
            # Operation.operation_id 列非空;复用时以行上的稳定值为准。
            operation_id: uuid.UUID = existing.operation_id
            await self._prepare_existing_for_execution(
                existing,
                adapter_key=adapter_key,
                expected_state=expected_state,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
            )
            await self._session.flush()
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
                expected_state=dict(expected_state),
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
        - NOT_APPLIED + ``retry_allowed`` → ToolCall 回 VALIDATING
          (必须重走完整 PreToolUse,不得直接重执行);
        - NOT_APPLIED 且不可重试 → ToolCall FAILED:确定失败落终态,不再重排队;
        - UNKNOWN → Operation/ToolCall UNKNOWN,Run ``RUNNING →
          WAITING_RECONCILIATION``,``next_step=RECONCILE``,禁止盲目重试;
        - 结果事务不校验 Run 是否仍 RUNNING:外部调用可能已经发生,回执必须
          落库;Run 状态竞争由 ``transition_run`` 的 ``state_version`` CAS 裁决。
        """
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

        tool_call_status = _tool_call_status_for(
            result.outcome, retry_allowed=result.retry_allowed
        )
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
                "stage": RECEIPT_STAGE_EXECUTION,
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
            next_step: RunNextStep | None = RunNextStep.RECONCILE
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
            next_step = None
        return RecordResultOutcome(
            tool_call_id=result.tool_call_id,
            operation_id=operation_row.operation_id,
            operation_outcome=result.outcome,
            tool_call_status=tool_call_status,
            retry_allowed=result.retry_allowed,
            run_status=run_status,
            next_step=next_step,
            event=event,
        )

    async def record_rejected_preparation(
        self,
        tool_call_id: uuid.UUID,
        *,
        adapter_key: str,
        rejection_context: Mapping[str, Any],
        error_code: str,
        request_fingerprint: str,
        supports_native_idempotency: bool = False,
        supports_reconciliation: bool = False,
        idempotency_key: str | None = None,
    ) -> RecordResultOutcome:
        """prepare 阶段确定性拒绝:直接落 NOT_APPLIED,不进入外部执行窗口。

        - 绝不写 EXECUTING、绝不写 TOOL_WRITE_ALLOWED、不新增 EXECUTE Attempt,
          因此不存在"可能已执行"的不确定窗口,也不会有回查被无谓触发;
        - ``expected_state`` 保持 NULL:拒绝不存在可回查的业务目标,把模型输入
          与策略版本这类审计上下文放进 ``last_receipt.rejection_context``;
        - ``last_receipt`` 带受控标记 ``stage=PREPARATION`` /
          ``kind=DETERMINISTIC_REJECTION``:这是"该 Operation 从未发起外部调用"
          的可验证证据,后续只有它允许在同一行上重新绑定目标;
        - **不覆盖已有执行历史**:若该 Operation 已经到过执行边界(存在 EXECUTE
          Attempt,或 ``last_receipt.stage=EXECUTION``),则保留原执行回执与完整
          ``expected_state``,不写 ``DETERMINISTIC_REJECTION`` 标记——否则一条确实
          调用过外部平台的行会被抹成"从未执行",并据此获得空目标重绑资格。此时
          新拒绝只写追加式 ``TOOL_WRITE_REJECTED`` 审计事件;
        - ``request_fingerprint`` 写进只追加的 TOOL_WRITE_REJECTED 审计事件——
          ``last_receipt`` 会被后续结果覆盖,不能当作不可变证据;
        - 拒绝一律不可重试:ToolCall 直接 ``FAILED``(确定性拒绝重排队只会再次
          走到同一拒绝);暂时性基础设施故障走独立的 retryable 回执契约;
        - Run 状态不变,不产生 RECONCILE Job。
        """
        await ensure_run_executable(
            self._session,
            workspace_id=self._workspace_id,
            run_id=self._run_id,
        )
        tool_call = await self._load_tool_call(tool_call_id)
        receipt = {
            "stage": RECEIPT_STAGE_PREPARATION,
            "kind": REJECTION_KIND_DETERMINISTIC,
            "outcome": OperationOutcome.NOT_APPLIED.value,
            "error_code": error_code,
            "external_reference": None,
            "payload": None,
            "rejection_context": dict(rejection_context),
        }
        assigned_id = tool_call.operation_id
        existing = (
            await self._load_operation_row(assigned_id) if assigned_id is not None else None
        )
        if existing is not None:
            # 只有尚未发起外部调用的 Operation 可以被改写为"确定未生效";
            # 已进入 EXECUTING/UNKNOWN/APPLIED 的行必须走 Reconciliation。
            if existing.status not in _REJECTION_RECORDABLE_STATUSES:
                raise OperationConflictError(
                    f"operation {existing.operation_id} 状态 {existing.status.value} "
                    "不允许记录 prepare 拒绝;已执行或不确定的操作只能走 Reconciliation"
                )
            # 执行能力由确认预约/首次执行边界落库;拒绝不是改写能力的理由。
            _require_same_execution_contract(
                existing,
                adapter_key=adapter_key,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
            )
            operation_id = existing.operation_id
            if not await self._operation_reached_execution_boundary(existing):
                # 从未发起外部调用(PREPARED 预约,或上一次也是本地拒绝):可以
                # 改写为"确定未生效"并留下受控来源标记,供后续空目标重绑。
                existing.status = OperationStatus.NOT_APPLIED
                existing.last_receipt = receipt
                await self._session.flush()
            # 已有执行历史:保留原执行回执与完整 expected_state;新拒绝只写
            # 追加式审计,绝不把"到过执行边界"抹成"从未进入执行边界"。
        else:
            operation_id = assigned_id if assigned_id is not None else uuid.uuid4()
            key = idempotency_key if idempotency_key is not None else f"op:{operation_id}"
            operation_row = Operation(
                workspace_id=self._workspace_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                idempotency_key=key,
                adapter_key=adapter_key,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
                expected_state=None,
                status=OperationStatus.NOT_APPLIED,
                last_receipt=receipt,
            )
            self._session.add(operation_row)
            await self._session.flush()
            if assigned_id is None:
                await self._assign_operation_id(tool_call_id, operation_id)
        tool_call_status = _tool_call_status_for(
            OperationOutcome.NOT_APPLIED, retry_allowed=False
        )
        if not await self._transition_tool_call(
            tool_call_id,
            new_status=tool_call_status,
            expected_statuses=frozenset(
                {ToolCallStatus.VALIDATING, ToolCallStatus.EXECUTING}
            ),
            failure_code=error_code,
        ):
            raise OperationConflictError(
                f"tool call {tool_call_id} 不处于 VALIDATING/EXECUTING;"
                " 无法记录 prepare 拒绝"
            )
        await self._audit.append(
            TOOL_WRITE_REJECTED,
            payload={
                "tool_call_id": str(tool_call_id),
                "operation_id": str(operation_id),
                "adapter_key": adapter_key,
                "error_code": error_code,
                "request_fingerprint": request_fingerprint,
                "retryable": False,
            },
            run_id=self._run_id,
            tool_call_id=tool_call_id,
            outcome=OUTCOME_FAILURE,
            reason_code=error_code,
        )
        event = await self._writer.append(
            self._run_id,
            EVENT_TOOL_RESULT_RECORDED,
            {
                "tool_call_id": str(tool_call_id),
                "operation_id": str(operation_id),
                "outcome": OperationOutcome.NOT_APPLIED.value,
                "error_code": error_code,
            },
            tool_call_id=tool_call_id,
        )
        return RecordResultOutcome(
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            operation_outcome=OperationOutcome.NOT_APPLIED,
            tool_call_status=tool_call_status,
            retry_allowed=False,
            run_status=await self._load_run_status(),
            next_step=None,
            event=event,
        )

    # ---- 内部 ----

    async def _prepare_existing_for_execution(
        self,
        operation_row: Operation,
        *,
        adapter_key: str,
        expected_state: Mapping[str, Any],
        supports_native_idempotency: bool,
        supports_reconciliation: bool,
    ) -> None:
        """复用已有 Operation 的执行前提校验与状态推进;任一不满足即 fail closed。"""
        status = operation_row.status
        if status not in _BEGINNABLE_OPERATION_STATUSES:
            raise OperationConflictError(
                f"operation {operation_row.operation_id} 状态 {status.value} "
                "不允许进入执行边界;已生效(APPLIED)或结果不确定(UNKNOWN/FAILED)"
                " 的操作只能走 Reconciliation"
            )
        if operation_row.expected_state is None:
            # 目标为空只允许在"确定从未发起外部调用"的 Operation 上首次绑定:
            # PREPARED,或带 DETERMINISTIC_REJECTION 标记的 NOT_APPLIED。其他
            # 来源出现 NULL 说明目标记录缺失:此时若放行,恢复方会把一个可能已
            # 执行的操作误判为"尚未绑定目标"→ 重复写。
            if not _target_binding_allowed(operation_row):
                raise OperationConflictError(
                    f"operation {operation_row.operation_id} 状态 {status.value} 下 "
                    "expected_state 为空且缺少 DETERMINISTIC_REJECTION 标记;"
                    "无法确认该操作从未发起外部调用,拒绝执行(fail closed)"
                )
            # 标记只是 prepare 拒绝写的诊断快照:必须再确认没有任何 EXECUTE
            # Attempt,否则一条到过执行边界(已发生过外部调用)的行会借标记
            # 重新绑定目标 → 重复写。
            if await self._operation_has_execute_attempt(operation_row.id):
                raise OperationConflictError(
                    f"operation {operation_row.operation_id} 已有 EXECUTE Attempt;"
                    "该操作到过执行边界,不允许在空目标下重新绑定(fail closed)"
                )
            # 目标为空不构成改写执行能力的理由:确认预约已经知道完整
            # execution_profile,能力差异只能说明记录被静默改写。
            _require_same_execution_contract(
                operation_row,
                adapter_key=adapter_key,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
            )
            operation_row.expected_state = dict(expected_state)
        else:
            persisted = canonical_expected_state(operation_row.expected_state)
            requested = canonical_expected_state(expected_state)
            if persisted != requested:
                raise OperationConflictError(
                    f"operation {operation_row.operation_id} 已持久化不同的 "
                    f"expected_state: persisted={persisted} requested={requested};"
                    " 同一 operation_id 不得改变业务目标"
                )
            _require_same_execution_contract(
                operation_row,
                adapter_key=adapter_key,
                supports_native_idempotency=supports_native_idempotency,
                supports_reconciliation=supports_reconciliation,
            )
        # 任何即将发起外部调用的 begin 都必须让 Operation 处于 EXECUTING:
        # 这是恢复/回收方识别"可能已执行"的唯一判据。
        operation_row.status = OperationStatus.EXECUTING

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

    async def _operation_has_execute_attempt(self, operation_row_id: uuid.UUID) -> bool:
        """该 Operation 是否到过执行边界(存在 EXECUTE Attempt)。

        prepare 拒绝从不新增 Attempt,任何 EXECUTE Attempt 都意味着 ``begin``
        曾把它推进 ``EXECUTING`` 并即将发起外部调用;因此它是"可能已执行"的
        只追加证据,用于阻止空目标重新绑定、以及拒绝路径改写已有执行来源。
        """
        stmt = (
            select(OperationAttempt.id)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
                OperationAttempt.kind == OperationAttemptKind.EXECUTE,
            )
            .limit(1)
        )
        return (await self._session.scalar(stmt)) is not None

    async def _operation_reached_execution_boundary(self, operation_row: Operation) -> bool:
        """该 Operation 是否已到过执行边界(可能已调用外部平台)。

        两种只追加证据取或:存在 EXECUTE Attempt,或 ``last_receipt.stage`` 为
        ``EXECUTION``。命中即不得再用 prepare 拒绝改写来源标记、也不得空目标重绑。
        """
        receipt = operation_row.last_receipt
        if (
            isinstance(receipt, Mapping)
            and receipt.get("stage") == RECEIPT_STAGE_EXECUTION
        ):
            return True
        return await self._operation_has_execute_attempt(operation_row.id)

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
        """下一个 Attempt 序号;唯一约束覆盖全部 kind,因此取全局 max+1。

        (计划 02 Task 9 修复:此前按 kind 计数,EXECUTE 与 RECONCILE 在同一
        Operation 上会生成相同 attempt_no 并违反
        ``uq_operation_attempts_operation_attempt``。)
        """
        del kind
        stmt = (
            select(sa.func.max(OperationAttempt.attempt_no))
            .select_from(OperationAttempt)
            .where(
                OperationAttempt.workspace_id == self._workspace_id,
                OperationAttempt.operation_row_id == operation_row_id,
            )
        )
        current = await self._session.scalar(stmt)
        return 1 if current is None else int(current) + 1

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


_OPERATION_STATUS_BY_OUTCOME: Final[dict[OperationOutcome, OperationStatus]] = {
    OperationOutcome.APPLIED: OperationStatus.APPLIED,
    OperationOutcome.NOT_APPLIED: OperationStatus.NOT_APPLIED,
    OperationOutcome.UNKNOWN: OperationStatus.UNKNOWN,
}


def _tool_call_status_for(
    outcome: OperationOutcome, *, retry_allowed: bool
) -> ToolCallStatus:
    """结果分类 + 显式重试声明 → ToolCall 终态。

    ``NOT_APPLIED`` 只说明"这次没生效",不说明"值得再试":可重试时回到
    ``VALIDATING`` 等重走完整 PreToolUse,否则直接 ``FAILED`` 落终态。UNKNOWN
    永远不进 VALIDATING(禁止盲目重试),只能由 Reconciliation 推进。
    """
    if outcome is OperationOutcome.APPLIED:
        return ToolCallStatus.SUCCEEDED
    if outcome is OperationOutcome.NOT_APPLIED:
        return ToolCallStatus.VALIDATING if retry_allowed else ToolCallStatus.FAILED
    return ToolCallStatus.UNKNOWN


_ATTEMPT_STATUS_BY_OUTCOME: Final[dict[OperationOutcome, OperationAttemptStatus]] = {
    OperationOutcome.APPLIED: OperationAttemptStatus.SUCCEEDED,
    OperationOutcome.NOT_APPLIED: OperationAttemptStatus.FAILED,
    # UNKNOWN:外部写入命运未卜,Attempt 既非成功也非失败。
    OperationOutcome.UNKNOWN: OperationAttemptStatus.ABANDONED,
}
