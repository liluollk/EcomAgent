"""AgentLoop:一次 Worker 推进 Run 的主循环(计划 02 Task 9;架构文档 §8)。

推进序列(架构文档 §8):

    加载 Run 与挂起状态 → 恢复语义:先处理挂起 ToolCall(重走完整
    PreToolUse)→ ContextAssembler 构造请求 → AgentBackend 流式生成
    (StreamingMessageRecorder 消费)→ 完整 ToolCall 按 ordinal 持久化为
    PROPOSED → 按模型顺序逐个进入 PreToolUse → 执行或挂起 → ToolResult
    回填后进入下一轮模型调用 → 无 ToolCall:Run COMPLETED。

决策分流(每个 ToolCall 独立执行 PreToolUse):

- ``ALLOW``:按 Run 固定 Tool 对应的 ToolBinding Handler 执行;写 Tool 走
  ToolExecutor 两阶段执行(要求有效 lease token);
- ``DENY``:ToolCall DENIED、拒绝事件落库、拒绝结果作为 ToolResult 回填,
  继续下一个调用(不中断循环);
- ``REQUIRE_INPUT`` / ``REQUIRE_CONFIRMATION``:经 InputService /
  ConfirmationService 创建 Request,Run 迁移 WAITING_INPUT /
  WAITING_CONFIRMATION,停止后续调用;
- 执行结果 ``UNKNOWN``:Run 已由 OperationService 迁移 WAITING_RECONCILIATION,
  Loop 停止后续并把 ``next_job_kind=RECONCILE_OPERATION`` 暴露在结果里;
  ``NOT_APPLIED`` 且回执显式声明可重试时 ToolCall 回 VALIDATING,Loop 停止并把
  ``next_job_kind=ADVANCE_RUN`` 暴露给编排层重新排队,恢复后重走 PreToolUse;
  不可重试的确定失败(含 prepare 阶段拒绝)ToolCall 直接 FAILED,ToolResult
  回填后由模型决定下一步,不再重排队。

预算(:class:`~runtime.orchestration.limits.RunLimits`):每轮模型调用前记账
模型轮次,每个新 ToolCall 处理前记账 ToolCall 数;轮次开始前检查 Run
Deadline;读 Tool 执行后检查单 Tool 超时(写 Tool 的不确定性由两阶段执行与
回查语义覆盖);reconciliation 次数由 Task 8 的 ReconciliationService 预算
执行,Loop 只传递。超限抛
:class:`~runtime.orchestration.limits.LoopLimitExceeded`,Run 以对应错误码
FAILED,不能继续自动循环。可重试 BackendFailed 重启流并创建新 RunAttempt;
不可重试 → Run FAILED。

事务边界:本服务按持久化检查点提交;每次状态变更与对应 AgentEvent 在同一
短事务内提交。所有模型/Tool 外部等待前先提交,流式事件落库后立即提交。
已知的模型可见信息
限制:ToolCall 表没有 provider call_id 列,恢复处理的挂起调用以 ToolCall
行 ID 作为 ToolResult 消息的 call_id。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, cast

import sqlalchemy as sa
from sqlalchemy import CursorResult, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.backend.protocol import (
    AgentBackend,
    BackendEvent,
    BackendFailed,
    BackendRequest,
    BackendToolCall,
    CancellationToken,
    ToolCallCompleted,
)
from runtime.capabilities.skill_activation import (
    LOAD_SKILL_KEY,
    SkillActivationError,
    SkillActivationService,
)
from runtime.events.writer import AgentEventWriter
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.operations import OperationOutcome
from runtime.execution.pretool.decisions import PreToolDecision
from runtime.execution.pretool.pipeline import ToolCallRequest
from runtime.execution.run_service import RunService
from runtime.execution.tool_executor import ToolExecutor
from runtime.execution.tool_registry import TOOL_UNKNOWN
from runtime.execution.tool_result_processor import (
    ProcessedToolResult,
    ToolExecutionReceipt,
    ToolResultProcessor,
)
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interaction.input_service import InputService
from runtime.interfaces.identifiers import ToolCallId
from runtime.interfaces.state import MessageType, RunStatus
from runtime.orchestration.context import ContextAssembler, ConversationView
from runtime.orchestration.context_budget import ContextBudgetExceeded
from runtime.orchestration.limits import LoopBudget, LoopLimitExceeded, RunLimits
from runtime.orchestration.messages import MessageStore
from runtime.orchestration.streaming import StreamingMessageRecorder
from runtime.persistence.base import (
    MessageRole,
    MessageStatus,
    OperationStatus,
    RunAttemptStatus,
    RunJobKind,
    ToolCallStatus,
    ToolEffect,
)
from runtime.persistence.models.execution import (
    Operation,
    Run,
    RunAttempt,
    ToolCall,
)
from runtime.persistence.models.interaction import Message
from runtime.persistence.repositories.runs import RunRepository


RUN_STARTED: Final = "RUN_STARTED"
RUN_COMPLETED: Final = "RUN_COMPLETED"
RUN_FAILED: Final = "RUN_FAILED"
RUN_WAITING_INPUT: Final = "RUN_WAITING_INPUT"
RUN_WAITING_CONFIRMATION: Final = "RUN_WAITING_CONFIRMATION"
TOOL_CALL_PROPOSED: Final = "TOOL_CALL_PROPOSED"
TOOL_CALL_DENIED: Final = "TOOL_CALL_DENIED"

CONTEXT_BUDGET_EXCEEDED: Final = "CONTEXT_BUDGET_EXCEEDED"
CONTEXT_ASSEMBLY_FAILED: Final = "CONTEXT_ASSEMBLY_FAILED"
TOOL_HANDLER_MISSING: Final = "TOOL_HANDLER_MISSING"
REQUIRE_INPUT_INVALID: Final = "REQUIRE_INPUT_INVALID"
WRITE_EXECUTION_PROFILE_MISSING: Final = "WRITE_EXECUTION_PROFILE_MISSING"

DEFAULT_INPUT_REQUEST_TTL: Final = timedelta(minutes=15)
DEFAULT_CONFIRMATION_TTL: Final = timedelta(minutes=30)

_PENDING_TOOL_CALL_STATUSES: Final[frozenset[ToolCallStatus]] = frozenset(
    {
        ToolCallStatus.PROPOSED,
        ToolCallStatus.VALIDATING,
        ToolCallStatus.WAITING_INPUT,
        ToolCallStatus.WAITING_CONFIRMATION,
    }
)


@dataclass(frozen=True)
class ToolPin:
    """Run 固定的 Tool 版本及其进程内 Handler、结果 Schema 与写执行元数据。

    ``execution_profile`` 只对写 Tool 有意义:写执行的 Adapter Key 与幂等/回查
    能力来自本 Tool 自身的绑定,不再由 Runtime/AgentLoop 全局持有。
    """

    stable_key: str
    version: int
    version_id: uuid.UUID
    effect: ToolEffect
    handler: Any | None = None
    output_schema: Mapping[str, Any] | None = None
    execution_profile: ToolExecutionProfile | None = None


class ToolPinResolver(Protocol):
    """进程内 Tool 版本查询:按稳定 Key 或版本 ID 解析 Run 已固定的 Tool。"""

    def by_stable_key(self, stable_key: str) -> ToolPin | None: ...

    def by_version_id(self, version_id: uuid.UUID) -> ToolPin | None: ...


class ToolCallEvaluator(Protocol):
    """PreToolUse 评估端点(:class:`PreToolUsePipeline` 满足该协议)。"""

    async def evaluate(self, tool_call: ToolCallRequest) -> PreToolDecision: ...


class ReadToolHandler(Protocol):
    """读 Tool 的外部执行端点(ToolBinding 装配;测试注入 fake)。"""

    async def read(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        deadline: datetime,
    ) -> Any: ...


@dataclass(frozen=True)
class AdvanceResult:
    """一次 ``advance`` 的推进结果(计划 Task 9 要求的公开字段)。

    ordinal 序列使用 list(与计划测试断言 ``== [0, 1]`` 字面一致)。
    """

    run_status: RunStatus
    persisted_ordinals: list[int] = field(default_factory=list)
    processed_ordinals: list[int] = field(default_factory=list)
    failure_code: str | None = None
    next_job_kind: RunJobKind | None = None


@dataclass(frozen=True)
class _Stop:
    """处理循环的停止指令;``failure_code`` 非空表示 Run 需确定性失败。"""

    run_status: RunStatus
    failure_code: str | None = None
    next_job_kind: RunJobKind | None = None


@dataclass(frozen=True)
class _RoundOutcome:
    """一轮模型调用的结果;``failure_code`` 非空表示 Run 需确定性失败。"""

    failure_code: str | None
    assistant_message_id: uuid.UUID | None
    tool_calls: tuple[ToolCallCompleted, ...]


class AgentLoop:
    """单线程 ToolCall 执行循环;在调用方事务内执行,只 flush 不 commit。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        backend: AgentBackend,
        assembler: ContextAssembler,
        limits: RunLimits,
        pipeline: ToolCallEvaluator,
        tool_pins: ToolPinResolver,
        processor: ToolResultProcessor,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        input_request_ttl: timedelta = DEFAULT_INPUT_REQUEST_TTL,
        confirmation_ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._backend = backend
        self._assembler = assembler
        self._limits = limits
        self._pipeline = pipeline
        self._tool_pins = tool_pins
        self._processor = processor
        self._checkpoint_callback = checkpoint
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._input_request_ttl = input_request_ttl
        self._confirmation_ttl = confirmation_ttl
        self._runs = RunRepository(session)
        self._run_service = RunService(session=session, workspace_id=workspace_id)
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)
        self._store = MessageStore(session=session, workspace_id=workspace_id)
        self._cancellation = CancellationToken()

    async def _checkpoint(self) -> None:
        """提交当前状态/Event 原子组,确保下一次外部等待不继承数据库事务。

        Runtime 显式提供生产提交策略;直接构造 Loop 的调用方默认只
        flush,可继续把更大的事务边界交给自己的 UnitOfWork。
        """
        if self._checkpoint_callback is None:
            await self._session.flush()
        else:
            await self._checkpoint_callback()

    # -- 交互服务(Run 级 Actor)----------------------------------------

    def _inputs_for(self, run: Run) -> InputService:
        """Input Request 的创建 Actor 是 Run 发起者。"""
        return InputService(
            self._session,
            workspace_id=self._workspace_id,
            actor_id=run.created_by_actor_id,
        )

    def _confirmations_for(self, run: Run) -> ConfirmationService:
        """Confirmation Request 的确认 Actor 是 Run 发起者。"""
        return ConfirmationService(
            self._session,
            workspace_id=self._workspace_id,
            actor_id=run.created_by_actor_id,
        )

    # -- 主入口 ---------------------------------------------------------

    async def advance(
        self,
        run_id: uuid.UUID,
        workspace_id: uuid.UUID | None = None,
        lease_token: uuid.UUID | None = None,
    ) -> AdvanceResult:
        """推进一个 Run 直到新的持久边界(终态/挂起/预算超限)。

        ``lease_token`` 是写 Tool 执行的 fencing 凭据;直接调用(测试、
        recover 路径)在 Run QUEUED 时会先完成 QUEUED→RUNNING 迁移。
        """
        if workspace_id is not None and workspace_id != self._workspace_id:
            raise ValueError("workspace_id mismatch with loop binding")
        run = await self._load_run(run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found in workspace {self._workspace_id}")
        current = RunStatus(run.status.value)
        if current.is_terminal or current in (
            RunStatus.WAITING_INPUT,
            RunStatus.WAITING_CONFIRMATION,
            RunStatus.WAITING_RECONCILIATION,
            RunStatus.CANCELLED,
        ):
            # 挂起 Run 的恢复由对应 Request/回查服务迁移后重新排队;终态不重放。
            return AdvanceResult(run_status=current, failure_code=run.failure_code)
        if current is RunStatus.QUEUED:
            await self._run_service.transition_run(
                run_id=run_id,
                target_status=RunStatus.RUNNING,
                event_kind=RUN_STARTED,
                event_payload={},
                expected_status=RunStatus.QUEUED,
            )
            run = await self._require_run(run_id)

        budget = await self._build_budget(run)
        persisted: list[int] = []
        processed: list[int] = []

        # 恢复语义:先处理挂起 ToolCall(重走完整 PreToolUse),再处理剩余 ordinal。
        for call in await self._load_pending_tool_calls(run.id):
            stop = await self._process_with_budget(
                run=run,
                call=call,
                budget=budget,
                lease_token=lease_token,
                processed=processed,
                record_budget=False,
            )
            resolved = await self._resolve_stop(run.id, stop, persisted, processed)
            if resolved is not None:
                return resolved

        while True:
            try:
                budget.ensure_run_deadline(now=self._clock())
                budget.record_model_round()
            except LoopLimitExceeded as limit:
                return await self._fail_run(run.id, limit.failure_code, persisted, processed)

            outcome = await self._model_round(run, budget)
            if outcome.failure_code is not None:
                return await self._fail_run(
                    run.id, outcome.failure_code, persisted, processed
                )
            fresh = await self._persist_completed_tool_calls(run, outcome)
            persisted.extend(call.ordinal for call in fresh)

            for call in fresh:
                stop = await self._process_with_budget(
                    run=run,
                    call=call,
                    budget=budget,
                    lease_token=lease_token,
                    processed=processed,
                    record_budget=True,
                )
                resolved = await self._resolve_stop(run.id, stop, persisted, processed)
                if resolved is not None:
                    return resolved

            # ToolResult 已持久化后必须回填给下一轮模型；只有纯文本轮次结束
            # 才能把 Run 标记为完成。
            if self._needs_model_followup(outcome):
                continue
            return await self._complete_run(run.id, persisted, processed)

    @staticmethod
    def _needs_model_followup(outcome: _RoundOutcome) -> bool:
        """只要模型产生过 ToolCall，就必须再进行一轮综合或后续决策。"""
        return bool(outcome.tool_calls)

    async def _resolve_stop(
        self,
        run_id: uuid.UUID,
        stop: _Stop | None,
        persisted: list[int],
        processed: list[int],
    ) -> AdvanceResult | None:
        if stop is None:
            return None
        if stop.failure_code is not None:
            return await self._fail_run(run_id, stop.failure_code, persisted, processed)
        return AdvanceResult(
            run_status=stop.run_status,
            persisted_ordinals=list(persisted),
            processed_ordinals=list(processed),
            next_job_kind=stop.next_job_kind,
        )

    # -- 预算与计数 -----------------------------------------------------

    async def _build_budget(self, run: Run) -> LoopBudget:
        """以 Run 上已持久化的计数初始化预算(恢复重入沿用同一预算)。"""
        assistant_rounds = await self._session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.workspace_id == self._workspace_id,
                Message.run_id == run.id,
                Message.role == MessageRole.ASSISTANT,
            )
        )
        tool_calls = await self._session.scalar(
            select(func.count())
            .select_from(ToolCall)
            .where(ToolCall.workspace_id == self._workspace_id, ToolCall.run_id == run.id)
        )
        return LoopBudget(
            self._limits,
            model_rounds_used=int(assistant_rounds or 0),
            tool_calls_used=int(tool_calls or 0),
            started_at=self._clock(),
            deadline_at=run.deadline_at,
        )

    # -- 模型轮次 -------------------------------------------------------

    async def _model_round(self, run: Run, budget: LoopBudget) -> _RoundOutcome:
        """一轮模型调用:装配上下文 → 流式消费 → 返回完整 ToolCall。"""
        try:
            await self._abort_interrupted_streams(run)
            built = self._assembler.build(await self._conversation_view(run))
            request = await built
        except ContextBudgetExceeded:
            # Protected 上下文超预算 fail closed:确定性失败,不盲目重试。
            return _RoundOutcome(
                failure_code=CONTEXT_BUDGET_EXCEEDED,
                assistant_message_id=None,
                tool_calls=(),
            )
        except Exception:  # noqa: BLE001 - 装配失败按确定性错误失败 Run
            return _RoundOutcome(
                failure_code=CONTEXT_ASSEMBLY_FAILED,
                assistant_message_id=None,
                tool_calls=(),
            )

        recorder = StreamingMessageRecorder(
            store=self._store,
            workspace_id=self._workspace_id,
            session_id=run.session_id,
            run_id=run.id,
            sequence_no=await self._next_message_sequence(run.session_id),
            checkpoint=self._checkpoint,
        )
        outcome = await recorder.consume(
            self._stream_with_retries(request, budget, run),
            self._cancellation,
        )
        if outcome.status == "ABORTED":
            # Provider 失败/取消/流提前终止:不可自动恢复,Run 以流错误码 FAILED。
            return _RoundOutcome(
                failure_code=outcome.error_code or "STREAM_ABORTED",
                assistant_message_id=outcome.message_id,
                tool_calls=(),
            )
        return _RoundOutcome(
            failure_code=None,
            assistant_message_id=outcome.message_id,
            tool_calls=outcome.tool_calls,
        )

    async def _abort_interrupted_streams(self, run: Run) -> None:
        """恢复时关闭上次 Worker 崩溃留下的部分 Assistant Message。"""
        rows = (
            await self._session.scalars(
                select(Message).where(
                    Message.workspace_id == self._workspace_id,
                    Message.run_id == run.id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.status == MessageStatus.STREAMING,
                )
            )
        ).all()
        if not rows:
            return
        for row in rows:
            row.status = MessageStatus.ABORTED
        await self._session.flush()
        await self._checkpoint()

    async def _stream_with_retries(
        self, request: BackendRequest, budget: LoopBudget, run: Run
    ) -> AsyncIterator[BackendEvent]:
        """可重试 BackendFailed:重启流并创建新 RunAttempt;不可重试原样透出。"""
        attempts = 0
        while True:
            failed: BackendFailed | None = None
            async for event in self._backend.stream(request, self._cancellation):
                if isinstance(event, BackendFailed):
                    failed = event
                    break
                yield event
            if failed is None:
                return
            if failed.retryable and attempts < budget.backend_retries:
                attempts += 1
                await self._create_retry_attempt(run)
                continue
            yield failed
            return

    async def _create_retry_attempt(self, run: Run) -> None:
        """可重试 Provider 错误:创建新 RunAttempt 后重试本次模型调用。"""
        attempt_no = await self._session.scalar(
            select(func.max(RunAttempt.attempt_no)).where(RunAttempt.run_id == run.id)
        )
        next_no = 1 if attempt_no is None else int(attempt_no) + 1
        self._session.add(
            RunAttempt(
                workspace_id=self._workspace_id,
                run_id=run.id,
                attempt_no=next_no,
                status=RunAttemptStatus.RUNNING,
                started_at=self._clock(),
            )
        )
        await self._session.flush()
        await self._checkpoint()

    async def _conversation_view(self, run: Run) -> ConversationView:
        """以已完成消息构造装配视图:仅当最后一条是用户消息时视作当前消息。"""
        messages = await self._store.list_completed_messages(session_id=run.session_id)
        active_skills = await SkillActivationService(
            self._session,
            workspace_id=self._workspace_id,
        ).active_for_run(run.id)
        current = (
            messages[-1] if messages and messages[-1].role is MessageType.USER else None
        )
        return ConversationView(
            messages=tuple(messages) if current is None else tuple(messages[:-1]),
            current_user_message=current,
            active_skill_instructions=active_skills,
        )

    async def _next_message_sequence(self, session_id: uuid.UUID) -> int:
        """会话内消息序号:max+1(uq_messages_session_sequence)。"""
        current = await self._session.scalar(
            select(func.max(Message.sequence_no)).where(Message.session_id == session_id)
        )
        return 1 if current is None else int(current) + 1

    # -- ToolCall 持久化 ------------------------------------------------

    async def _persist_completed_tool_calls(
        self, run: Run, outcome: _RoundOutcome
    ) -> list[ToolCall]:
        """先把本轮全部完整 ToolCall 按 ordinal 持久化为 PROPOSED(§8)。"""
        assert outcome.assistant_message_id is not None
        rows: list[ToolCall] = []
        history_tool_calls: list[BackendToolCall] = []
        for event in outcome.tool_calls:
            pin = self._tool_pins.by_stable_key(event.name)
            if pin is None:
                # 未固定/未注册的 Tool:拒绝并回填 ToolResult,不持久化行。
                await self._writer.append(
                    run.id,
                    TOOL_CALL_DENIED,
                    {
                        "call_id": event.call_id,
                        "tool_name": event.name,
                        "reason_code": TOOL_UNKNOWN,
                    },
                )
                history_tool_calls.append(
                    BackendToolCall(
                        id=event.call_id,
                        name=event.name,
                        arguments=dict(event.arguments),
                    )
                )
                await self._append_tool_message(
                    run, call_id=event.call_id, tool_name=event.name, content=TOOL_UNKNOWN
                )
                continue
            ordinal = await self._next_tool_call_ordinal(run.id)
            row = ToolCall(
                workspace_id=self._workspace_id,
                run_id=run.id,
                message_id=outcome.assistant_message_id,
                tool_version_id=pin.version_id,
                ordinal=ordinal,
                status=ToolCallStatus.PROPOSED,
                raw_arguments=dict(event.arguments),
            )
            self._session.add(row)
            await self._session.flush()
            await self._writer.append(
                run.id,
                TOOL_CALL_PROPOSED,
                {"call_id": event.call_id, "tool_name": event.name, "ordinal": ordinal},
                tool_call_id=row.id,
            )
            history_tool_calls.append(
                BackendToolCall(
                    id=str(row.id),
                    name=event.name,
                    arguments=dict(event.arguments),
                )
            )
            rows.append(row)
        if history_tool_calls:
            await self._store.set_assistant_tool_calls(
                outcome.assistant_message_id, tuple(history_tool_calls)
            )
        return rows

    async def _next_tool_call_ordinal(self, run_id: uuid.UUID) -> int:
        """Run 内 ToolCall ordinal:max+1(uq_tool_calls_run_ordinal,跨恢复连续)。"""
        current = await self._session.scalar(
            select(func.max(ToolCall.ordinal)).where(
                ToolCall.workspace_id == self._workspace_id, ToolCall.run_id == run_id
            )
        )
        return 0 if current is None else int(current) + 1

    # -- 顺序处理 -------------------------------------------------------

    async def _process_with_budget(
        self,
        *,
        run: Run,
        call: ToolCall,
        budget: LoopBudget,
        lease_token: uuid.UUID | None,
        processed: list[int],
        record_budget: bool,
    ) -> _Stop | None:
        """处理单个 ToolCall;返回 ``_Stop`` 表示挂起/失败,需停止后续调用。"""
        if record_budget:
            # 恢复的挂起调用已计入初始用量,只有新调用才记账。
            try:
                budget.record_tool_call()
            except LoopLimitExceeded as limit:
                return _Stop(RunStatus.RUNNING, failure_code=limit.failure_code)
        processed.append(call.ordinal)
        try:
            return await self._process_tool_call(
                run=run, call=call, budget=budget, lease_token=lease_token
            )
        except LoopLimitExceeded as limit:
            return _Stop(RunStatus.RUNNING, failure_code=limit.failure_code)

    async def _process_tool_call(
        self,
        *,
        run: Run,
        call: ToolCall,
        budget: LoopBudget,
        lease_token: uuid.UUID | None,
    ) -> _Stop | None:
        """单个 ToolCall:PreToolUse 决策分流(架构文档 §8、§9)。"""
        pin = self._require_pin(call)
        # PROPOSED / 挂起状态 → VALIDATING;恢复重入必须重走完整 PreToolUse。
        await self._mark_tool_call(call, ToolCallStatus.VALIDATING)
        request = ToolCallRequest(
            tool_call_id=ToolCallId(call.id),
            stable_key=pin.stable_key,
            version=pin.version,
            raw_arguments=json.dumps(self._base_arguments(call)),
        )
        decision = await self._pipeline.evaluate(request)

        if decision.normalized_arguments is not None:
            # ToolCall 参数存入 JSONB;Decimal 等领域类型必须在持久化边界
            # 转成无损 JSON 标量。ALLOW/恢复路径继续复用，DENY 路径保留
            # 目标证据供审计与 Safety 统计，不改变本次决策结果。
            call.normalized_arguments = json.loads(
                json.dumps(dict(decision.normalized_arguments), default=str)
            )
            await self._session.flush()

        if decision.kind == "DENY":
            return await self._handle_deny(run, call, pin, decision)
        if decision.kind == "REQUIRE_INPUT":
            return await self._handle_require_input(run, call, pin, decision)
        if decision.kind == "REQUIRE_CONFIRMATION":
            return await self._handle_require_confirmation(run, call, pin, decision)
        return await self._handle_allow(run, call, pin, budget, lease_token)

    @staticmethod
    def _base_arguments(call: ToolCall) -> dict[str, Any]:
        """进入 PreToolUse 的参数:已回答字段合并后的规范化参数优先。"""
        merged = (
            call.normalized_arguments
            if call.normalized_arguments is not None
            else call.raw_arguments
        )
        return dict(merged)

    async def _handle_deny(
        self, run: Run, call: ToolCall, pin: ToolPin, decision: PreToolDecision
    ) -> _Stop | None:
        """DENY 不中断循环:记录拒绝事件、回填拒绝结果,继续下一个调用。"""
        await self._mark_tool_call(
            call, ToolCallStatus.DENIED, failure_code=decision.reason_code
        )
        await self._writer.append(
            run.id,
            TOOL_CALL_DENIED,
            {
                "tool_call_id": str(call.id),
                "tool_name": pin.stable_key,
                "reason_code": decision.reason_code,
            },
            tool_call_id=call.id,
        )
        safe_message = str(decision.detail.get("safe_message") or decision.reason_code)
        await self._append_tool_message(
            run, call_id=str(call.id), tool_name=pin.stable_key, content=safe_message
        )
        return None

    async def _handle_require_input(
        self, run: Run, call: ToolCall, pin: ToolPin, decision: PreToolDecision
    ) -> _Stop | None:
        """创建 Input Request,Run WAITING_INPUT,停止后续调用。"""
        requirement = decision.input_requirement
        diagnostic = decision.schema_diagnostic
        field_paths = (
            tuple(requirement.field_paths)
            if requirement is not None and requirement.field_paths
            else (diagnostic.field_paths if diagnostic is not None else ())
        )
        if not field_paths:
            # 无可回答字段:确定性拒绝,不生成空 Request。
            return await self._handle_deny(
                run, call, pin, PreToolDecision(kind="DENY", reason_code=REQUIRE_INPUT_INVALID)
            )
        question = (
            (requirement.question if requirement is not None else None)
            or (diagnostic.message if diagnostic is not None else None)
            or "additional information required"
        )
        response_schema = (
            dict(requirement.response_schema) if requirement is not None else {}
        )
        # 服务端候选项随 Request 持久化:Workbench/API 直接读取,不再各自重算;
        # 未提供候选项(如纯 Schema 缺失)时保持 None。
        options = (
            dict(requirement.options)
            if requirement is not None and requirement.options
            else None
        )
        await self._inputs_for(run).create(
            session_id=run.session_id,
            run_id=run.id,
            field_paths=list(field_paths),
            question=question,
            response_schema=response_schema,
            expires_at=self._clock() + self._input_request_ttl,
            tool_call_id=call.id,
            options=options,
        )
        await self._mark_tool_call(call, ToolCallStatus.WAITING_INPUT)
        await self._run_service.transition_run(
            run_id=run.id,
            target_status=RunStatus.WAITING_INPUT,
            event_kind=RUN_WAITING_INPUT,
            event_payload={"tool_call_id": str(call.id)},
            expected_status=RunStatus.RUNNING,
        )
        return _Stop(RunStatus.WAITING_INPUT)

    async def _handle_require_confirmation(
        self, run: Run, call: ToolCall, pin: ToolPin, decision: PreToolDecision
    ) -> _Stop | None:
        """创建 Confirmation Request,Run WAITING_CONFIRMATION,停止后续调用。

        写 Tool 在首次进入 Confirmation 前分配稳定 operation_id(不再重新生成)。
        """
        spec = decision.confirmation_spec
        arguments = (
            dict(spec.normalized_arguments)
            if spec is not None and spec.normalized_arguments
            else self._base_arguments(call)
        )
        operation_id = call.operation_id
        if operation_id is None and pin.effect is ToolEffect.WRITE:
            if pin.execution_profile is None:
                # 写 Tool 缺执行 Profile:无法登记 Operation,确定性失败而不是
                # 生成无法执行的 Confirmation。
                return _Stop(
                    RunStatus.RUNNING, failure_code=WRITE_EXECUTION_PROFILE_MISSING
                )
            operation_id = uuid.uuid4()
            await self._assign_operation_id(call, operation_id, pin.execution_profile)
        await self._confirmations_for(run).create(
            session_id=run.session_id,
            run_id=run.id,
            tool_call_id=call.id,
            normalized_arguments=arguments,
            resource_versions=dict(spec.resource_versions) if spec is not None else {},
            expires_at=self._clock() + self._confirmation_ttl,
            operation_id=operation_id,
            tool_schema_version=pin.version,
        )
        await self._mark_tool_call(call, ToolCallStatus.WAITING_CONFIRMATION)
        await self._run_service.transition_run(
            run_id=run.id,
            target_status=RunStatus.WAITING_CONFIRMATION,
            event_kind=RUN_WAITING_CONFIRMATION,
            event_payload={"tool_call_id": str(call.id)},
            expected_status=RunStatus.RUNNING,
        )
        return _Stop(RunStatus.WAITING_CONFIRMATION)

    async def _handle_allow(
        self,
        run: Run,
        call: ToolCall,
        pin: ToolPin,
        budget: LoopBudget,
        lease_token: uuid.UUID | None,
    ) -> _Stop | None:
        """ALLOW:读 Tool 直接执行;写 Tool 走两阶段 ToolExecutor。"""
        if pin.effect is ToolEffect.READ:
            return await self._execute_read(
                run, call, pin, budget, self._base_arguments(call)
            )
        return await self._execute_write(
            run, call, pin, lease_token, self._base_arguments(call)
        )

    async def _execute_read(
        self,
        run: Run,
        call: ToolCall,
        pin: ToolPin,
        budget: LoopBudget,
        arguments: dict[str, Any],
    ) -> _Stop | None:
        """读 Tool:注入 handler 执行 + 单 Tool 超时 + PostToolUse 处理。"""
        started_at = self._clock()
        deadline = budget.tool_deadline(now=started_at)
        if pin.stable_key == LOAD_SKILL_KEY:
            receipt = await self._load_skill_receipt(run, call, arguments)
        else:
            handler = pin.handler
            if handler is None or not callable(getattr(handler, "read", None)):
                return _Stop(RunStatus.RUNNING, failure_code=TOOL_HANDLER_MISSING)
            await self._checkpoint()
            receipt = await handler.read(
                tool_call_id=call.id,
                normalized_arguments=arguments,
                deadline=deadline,
            )
        try:
            budget.ensure_tool_timeout(started_at=started_at, now=self._clock())
        except LoopLimitExceeded as limit:
            await self._mark_tool_call(
                call, ToolCallStatus.FAILED, failure_code=limit.failure_code
            )
            return _Stop(RunStatus.RUNNING, failure_code=limit.failure_code)
        processed = await self._processor.process(
            receipt, output_schema=pin.output_schema
        )
        await self._finish_read_tool_call(call, processed)
        await self._append_tool_message(
            run,
            call_id=str(call.id),
            tool_name=pin.stable_key,
            content=_projection_content(processed),
        )
        await self._checkpoint()
        return None

    async def _load_skill_receipt(
        self,
        run: Run,
        call: ToolCall,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionReceipt:
        """执行 Runtime 内置 `load_skill`，不下沉到业务 ReadToolHandler。"""
        skill_key = arguments.get("skill_key")
        if not isinstance(skill_key, str) or not skill_key.strip():
            return ToolExecutionReceipt(
                tool_call_id=call.id,
                status="FAILED",
                error_code="INVALID_SKILL_KEY",
            )
        try:
            activated = await SkillActivationService(
                self._session,
                workspace_id=self._workspace_id,
            ).activate(run.id, skill_key.strip())
        except SkillActivationError as error:
            return ToolExecutionReceipt(
                tool_call_id=call.id,
                status="FAILED",
                error_code=type(error).__name__.upper(),
            )
        return ToolExecutionReceipt(
            tool_call_id=call.id,
            status="SUCCEEDED",
            payload=activated.tool_payload(),
        )

    async def _execute_write(
        self,
        run: Run,
        call: ToolCall,
        pin: ToolPin,
        lease_token: uuid.UUID | None,
        arguments: dict[str, Any],
    ) -> _Stop | None:
        """写 Tool:begin → 外部调用 → PostToolUse → 结果事务(含 fencing)。"""
        handler = pin.handler
        if handler is None or not callable(getattr(handler, "execute_prepared", None)):
            return _Stop(RunStatus.RUNNING, failure_code=TOOL_HANDLER_MISSING)
        execution_profile = pin.execution_profile
        if execution_profile is None:
            return _Stop(
                RunStatus.RUNNING, failure_code=WRITE_EXECUTION_PROFILE_MISSING
            )
        if lease_token is None:
            raise RuntimeError(
                "write tool execution requires an active lease_token for fencing"
            )
        fresh = await self._require_run(run.id)
        executor = ToolExecutor(
            self._session,
            workspace_id=self._workspace_id,
            actor_id=run.created_by_actor_id,
            run_id=run.id,
            lease_token=lease_token,
            expected_state_version=fresh.state_version,
            handler=handler,
            processor=self._processor,
            checkpoint=self._checkpoint,
            output_schema=pin.output_schema,
            execution_profile=execution_profile,
        )
        executed = await executor.execute(
            tool_call_id=call.id, normalized_arguments=arguments
        )
        recorded = executed.recorded
        if recorded.operation_outcome is OperationOutcome.UNKNOWN:
            # Run 已被 OperationService 迁移 WAITING_RECONCILIATION;停止后续并把
            # Task 8 的 next_job_kind 指令暴露给编排层。
            return _Stop(
                RunStatus.WAITING_RECONCILIATION,
                next_job_kind=recorded.next_job_kind,
            )
        if recorded.tool_call_status is ToolCallStatus.VALIDATING:
            # NOT_APPLIED + retry_allowed:重新排队,恢复后重走完整 PreToolUse。
            return _Stop(RunStatus.RUNNING, next_job_kind=RunJobKind.ADVANCE_RUN)
        # APPLIED 与不可重试的确定失败都回填 Tool Result 让模型决定下一步:
        # NOT_APPLIED 且 retry_allowed=False 时 ToolCall 已是 FAILED 终态,
        # 不会再次进入 PreToolUse(确定性拒绝只执行一次)。
        await self._append_tool_message(
            run,
            call_id=str(call.id),
            tool_name=pin.stable_key,
            content=_projection_content(executed.processed),
        )
        return None

    # -- 终态迁移 -------------------------------------------------------

    async def _complete_run(
        self, run_id: uuid.UUID, persisted: list[int], processed: list[int]
    ) -> AdvanceResult:
        await self._run_service.transition_run(
            run_id=run_id,
            target_status=RunStatus.COMPLETED,
            event_kind=RUN_COMPLETED,
            event_payload={},
            expected_status=RunStatus.RUNNING,
        )
        await self._session.execute(
            sa.update(Run)
            .where(Run.workspace_id == self._workspace_id, Run.id == run_id)
            .values(completed_at=self._clock())
            .execution_options(synchronize_session=False)
        )
        return AdvanceResult(
            run_status=RunStatus.COMPLETED,
            persisted_ordinals=list(persisted),
            processed_ordinals=list(processed),
        )

    async def _fail_run(
        self,
        run_id: uuid.UUID,
        failure_code: str,
        persisted: list[int],
        processed: list[int],
    ) -> AdvanceResult:
        """确定性失败:Run RUNNING → FAILED 并落 failure_code 列。"""
        await self._run_service.transition_run(
            run_id=run_id,
            target_status=RunStatus.FAILED,
            event_kind=RUN_FAILED,
            event_payload={"failure_code": failure_code},
            expected_status=RunStatus.RUNNING,
        )
        await self._session.execute(
            sa.update(Run)
            .where(Run.workspace_id == self._workspace_id, Run.id == run_id)
            .values(failure_code=failure_code)
            .execution_options(synchronize_session=False)
        )
        return AdvanceResult(
            run_status=RunStatus.FAILED,
            persisted_ordinals=list(persisted),
            processed_ordinals=list(processed),
            failure_code=failure_code,
        )

    # -- 持久化辅助 -----------------------------------------------------

    async def _load_run(self, run_id: uuid.UUID) -> Run | None:
        return await self._runs.get(self._workspace_id, run_id)

    async def _require_run(self, run_id: uuid.UUID) -> Run:
        run = await self._runs.get(self._workspace_id, run_id)
        assert run is not None
        return run

    def _require_pin(self, call: ToolCall) -> ToolPin:
        pin = self._tool_pins.by_version_id(call.tool_version_id)
        if pin is None:
            raise LookupError(
                f"tool version {call.tool_version_id} of tool call {call.id} is not pinned"
            )
        return pin

    async def _load_pending_tool_calls(self, run_id: uuid.UUID) -> list[ToolCall]:
        """挂起(未达执行终态)的 ToolCall,按 ordinal 升序。"""
        stmt = (
            select(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.run_id == run_id,
                ToolCall.status.in_(
                    [status.value for status in _PENDING_TOOL_CALL_STATUSES]
                ),
            )
            .order_by(ToolCall.ordinal)
        )
        return list((await self._session.scalars(stmt)).all())

    async def _mark_tool_call(
        self,
        call: ToolCall,
        target: ToolCallStatus,
        *,
        failure_code: str | None = None,
    ) -> None:
        """条件更新 ToolCall 状态(可恢复态 → 目标态);零行更新抛冲突错误。"""
        stmt = (
            sa.update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == call.id,
                ToolCall.status.in_(
                    [status.value for status in _PENDING_TOOL_CALL_STATUSES]
                ),
            )
            .values(status=target.value, failure_code=failure_code)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        if result.rowcount != 1:
            raise RuntimeError(
                f"tool call {call.id} is not resumable; cannot transition to {target.value}"
            )
        call.status = target

    async def _finish_read_tool_call(
        self, call: ToolCall, processed: ProcessedToolResult
    ) -> None:
        """读 Tool 无 Operation:按 PostToolUse 分类直接落到执行终态。"""
        succeeded = processed.operation_outcome.value == "APPLIED"
        stmt = (
            sa.update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == call.id,
                ToolCall.status == ToolCallStatus.VALIDATING.value,
            )
            .values(
                status=(
                    ToolCallStatus.SUCCEEDED.value
                    if succeeded
                    else ToolCallStatus.FAILED.value
                ),
                failure_code=processed.tool_call_error,
            )
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        if result.rowcount != 1:
            raise RuntimeError(
                f"tool call {call.id} is not VALIDATING; cannot record read result"
            )
        call.status = (
            ToolCallStatus.SUCCEEDED if succeeded else ToolCallStatus.FAILED
        )

    async def _assign_operation_id(
        self,
        call: ToolCall,
        operation_id: uuid.UUID,
        execution_profile: ToolExecutionProfile,
    ) -> None:
        """写 Tool 首次进入 Confirmation 前分配稳定 operation_id(只分配一次)。

        ``tool_calls.operation_id`` 对 operations 表有复合外键,因此同时创建
        PREPARED Operation 行(代表已分配 ID,不代表已发起外部写入);执行
        边界 ``OperationService.begin`` 幂等复用同一行并推进 EXECUTING。
        Adapter Key 与完整的幂等/回查能力都来自该写 Tool 自己的执行 Profile:
        预约阶段就落库完整能力,执行边界只做一致性校验,不接管能力写入。
        """
        self._session.add(
            Operation(
                workspace_id=self._workspace_id,
                tool_call_id=call.id,
                operation_id=operation_id,
                idempotency_key=f"op:{operation_id}",
                adapter_key=execution_profile.adapter_key,
                supports_native_idempotency=(
                    execution_profile.supports_native_idempotency
                ),
                supports_reconciliation=execution_profile.supports_reconciliation,
                status=OperationStatus.PREPARED,
            )
        )
        await self._session.flush()
        stmt = (
            sa.update(ToolCall)
            .where(
                ToolCall.workspace_id == self._workspace_id,
                ToolCall.id == call.id,
                ToolCall.operation_id.is_(None),
            )
            .values(operation_id=operation_id)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(stmt)
        call.operation_id = operation_id

    async def _append_tool_message(
        self,
        run: Run,
        *,
        call_id: str | None,
        tool_name: str | None,
        content: str,
    ) -> None:
        """ToolResult 以 TOOL 消息回填(下一轮模型调用可见)。"""
        await self._store.append_tool_message(
            session_id=run.session_id,
            run_id=run.id,
            sequence_no=await self._next_message_sequence(run.session_id),
            call_id=call_id,
            tool_name=tool_name,
            content=content,
        )


def _projection_content(processed: ProcessedToolResult) -> str:
    """模型可见的脱敏投影序列化为 ToolResult 内容。

    除数据本体外固定带上 ``operation_outcome`` 与 ``error_code``:写入确定失败
    后模型必须能看到"为什么没成功",否则只能收到空 ``{}`` 而无法决定下一步。
    """
    return json.dumps(
        {
            "operation_outcome": processed.operation_outcome.value,
            "error_code": processed.tool_call_error,
            "data": dict(processed.context_projection.structured_data),
            "text": processed.context_projection.text,
            "truncated": processed.context_projection.truncated,
        },
        ensure_ascii=False,
    )


__all__ = [
    "AdvanceResult",
    "AgentLoop",
    "ReadToolHandler",
    "ToolCallEvaluator",
    "ToolPin",
    "ToolPinResolver",
]
