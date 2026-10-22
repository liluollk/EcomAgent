"""Eval Runner(计划 05 Task 2 Step 3)。

:class:`EvalRunner` 按 case/repetition 逐个执行 Scenario:每例经
:class:`eval.fixtures.IsolatedEnvironment` 组装独立权威环境,以注入的
Backend 驱动真实 Runtime 推进(start → 缺店铺 Input → 确认 → 终态),
捕获完整 AgentEvent/ToolCall/Operation 与权威最终状态到
:class:`eval.results.CaseResult`。判分(任务成败)不属 Runner,由 Scorer
(Task 3)消费捕获结果。

确定性分类规则(不猜测):

- ``INFRASTRUCTURE_ERROR``:注入 Backend 抛 :class:`EvalProviderError`
  (如 401)、连接失败(``OSError`` 系,含 ``ConnectionError``/``TimeoutError``)
  或依赖不可用(``sqlalchemy`` ``InterfaceError``/``OperationalError``)——
  这类失败与被测 Agent 行为无关,``included_in_quality_score=False``。
  执行器会把推进异常归入 Run/Attempt 的失败码(Run 可能回 QUEUED),
  因此 Backend 异常经观察代理(``_ObservingBackend``)在流边界记录、于
  Run 停滞时原样重抛,按真实异常类型分类;
- ``FAILED``:Agent/Runtime 执行失败(若 Workspace/Run 已建立则计入任务完成率,
  作为失败计数;环境创建前失败因无可靠场景轨迹而排除);
- ``SKIPPED``:``run`` 的 ``skip_ids`` 显式注入;任一 SKIPPED 使整轮
  ``protocol_valid=False``;
- ``COMPLETED``:用例端到端执行完成(不代表任务成功),计入质量分母。

顺序无关:``run`` 内部按 case_id 稳定排序执行,结果与调用顺序解耦;
跨执行顺序的比较经 :func:`eval.results.by_case_id` 的内容投影完成。

驱动边界(Task 2):驱动首条用户消息的完整确认链路;多轮对话推进、
WAITING_RECONCILIATION 回查驱动属后续任务(Runner 在这些状态按确定性
失败处理,不静默假设成功)。
"""

from __future__ import annotations

import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
    Collection,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from composition.product_operations import ProductApplication
from eval.capability_manifest import EvalCapabilityManifest
from eval.config import EvalConfig
from eval.fixtures import IsolatedEnvironment
from eval.results import (
    CaseResult,
    CaseStatus,
    EvalRoundResult,
    EventCapture,
    FinalState,
    MessageCapture,
    OperationCapture,
    ToolCallCapture,
)
from eval.schema import Scenario, UserInteraction, protocol_fingerprint
from runtime.backend.protocol import (
    AgentBackend,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import ExecutionMode
from runtime.persistence.models.configuration import ToolDefinition, ToolVersion
from runtime.persistence.models.events import AgentEvent
from runtime.persistence.models.execution import Operation, Run, ToolCall
from runtime.persistence.models.interaction import InputRequest, Message


class EvalProviderError(Exception):
    """注入 Provider 的确定性失败(如 401);``status`` 供诊断,不改变分类。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


_INFRASTRUCTURE_EXCEPTIONS: Final[tuple[type[BaseException], ...]] = (
    EvalProviderError,
    # 连接失败:OSError 系(ConnectionError/TimeoutError 均为其子类)。
    OSError,
    # 依赖不可用:数据库连接/接口层错误。
    InterfaceError,
    OperationalError,
)

_TERMINAL_RUN_STATUSES: Final[frozenset[RunStatus]] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)
_PROVIDER_FAILURE_CODES: Final[frozenset[str]] = frozenset(
    {
        "AUTH_FAILED",
        "INVALID_REQUEST",
        "PROVIDER_CONNECTION_ERROR",
        "PROVIDER_ERROR",
        "PROVIDER_UNAVAILABLE",
        "RATE_LIMITED",
        "TIMEOUT",
    }
)

_MAX_DRIVE_STEPS: Final = 12
"""单 case 驱动步数上限(input/confirm 恢复推进次数的确定性上界)。"""


@dataclass
class _DriveState:
    """一次驱动的可观测状态(异常路径下仍可 best-effort 捕获)。"""

    run_id: uuid.UUID | None = None
    run_ids: list[uuid.UUID] | None = None
    session_id: uuid.UUID | None = None
    current_message_index: int | None = None
    observed: _ObservingBackend | None = None


class _ObservingBackend:
    """Backend 代理:记录 ``stream`` 抛出的异常并原样续传事件。

    必要性:执行器会吞掉推进回调的异常并把 Run 退回 QUEUED,Provider 失败
    的原始异常因此不会传到驱动方。本代理
    在 Provider 边界记录真实异常类型,供驱动循环在 Run 停滞时重新抛出,
    实现 deterministic 的基础设施失败分类(不猜测)。
    """

    def __init__(self, inner: AgentBackend) -> None:
        self._inner = inner
        self.recorded: list[BaseException] = []

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        try:
            async for event in self._inner.stream(request, cancellation):
                yield event
        except Exception as error:
            self.recorded.append(error)
            raise


@dataclass(frozen=True)
class _Capture:
    """一次 Run 的捕获结果(CaseResult 字段载荷)。"""

    events: tuple[EventCapture, ...] = ()
    conversation_messages: tuple[MessageCapture, ...] = ()
    tool_calls: tuple[ToolCallCapture, ...] = ()
    operations: tuple[OperationCapture, ...] = ()
    final_state: FinalState | None = None


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _symbolize_tool_arguments(
    environment: IsolatedEnvironment, arguments: Mapping[str, Any]
) -> dict[str, Any]:
    """将 Eval Fixture 的内部店铺 UUID 投影回场景符号键。

    Runtime 和数据库继续使用 UUID；只有 CaseResult 捕获使用稳定符号，供
    ``allowed_side_effects`` / ``forbidden_side_effects`` 跨重复比较。
    """
    captured = dict(arguments)
    store_id = captured.get("store_id")
    if isinstance(store_id, str):
        symbol = environment.store_symbol_for_id(store_id)
        if symbol is not None:
            captured["store_id"] = symbol
    return captured


class EvalRunner:
    """按 case/repetition 执行 Scenario 的评测运行器。

    ``backend_factory`` 为每个 case 创建注入 Backend(确定性 Scripted 或
    真实 Provider);``run_one`` 允许逐例覆盖 Backend(如基础设施失败注入)。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        config: EvalConfig,
        manifest: EvalCapabilityManifest,
        backend_factory: Callable[[Scenario], AgentBackend],
        repetitions: int | None = None,
        cleanup_environments: bool = True,
        execution_mode: ExecutionMode = ExecutionMode.AUTO_EXECUTE,
    ) -> None:
        self._engine = engine
        self.config = config
        """固定运行配置(协议指纹的配置分量;只读暴露)。"""
        self.manifest = manifest
        """整轮 Eval 共用的不可变逻辑能力快照。"""
        self._backend_factory = backend_factory
        self._repetitions = config.repetitions if repetitions is None else repetitions
        self._cleanup_environments = cleanup_environments
        self._execution_mode = execution_mode

    # -- 执行入口 ---------------------------------------------------------

    async def run(
        self,
        cases: Sequence[Scenario],
        *,
        skip_ids: Collection[str] | None = None,
    ) -> EvalRoundResult:
        """执行整轮评测:按 case_id 稳定排序,逐 case/repetition 推进。"""
        skips = frozenset(skip_ids) if skip_ids is not None else frozenset[str]()
        results: list[CaseResult] = []
        for case in sorted(cases, key=lambda scenario: scenario.id):
            for repetition in range(self._repetitions):
                if case.id in skips:
                    results.append(self._skipped_result(case, repetition=repetition))
                    continue
                results.append(await self.run_one(case, repetition=repetition))
        return EvalRoundResult(results=tuple(results))

    async def run_one(
        self,
        case: Scenario,
        *,
        backend: AgentBackend | None = None,
        repetition: int = 0,
    ) -> CaseResult:
        """执行单个 case/repetition 并返回完整捕获结果。

        环境创建、推进与捕获中的基础设施失败单独分类;其余意外异常按
        FAILED 处理;两种失败路径均 best-effort 捕获已产生的持久痕迹。
        """
        fingerprint = protocol_fingerprint(case, self.config, self.manifest)
        resolved_backend = backend if backend is not None else self._backend_factory(case)
        observing = _ObservingBackend(resolved_backend)
        state = _DriveState(observed=observing, run_ids=[])
        try:
            environment = await IsolatedEnvironment.create(
                self._engine,
                case,
                backend=observing,
                manifest=self.manifest,
                execution_mode=self._execution_mode,
            )
        except _INFRASTRUCTURE_EXCEPTIONS as error:
            return self._result_of_failure(
                case,
                repetition=repetition,
                fingerprint=fingerprint,
                status="INFRASTRUCTURE_ERROR",
                error=error,
            )
        except Exception as error:  # noqa: BLE001 - 种子/组装意外失败按 FAILED 记录
            return self._result_of_failure(
                case,
                repetition=repetition,
                fingerprint=fingerprint,
                status="FAILED",
                error=error,
            )

        try:
            await self._drive(environment, case, state)
            capture = await self._capture(environment, state.run_ids or ())
        except _INFRASTRUCTURE_EXCEPTIONS as error:
            return await self._failure_result(
                environment,
                case,
                repetition=repetition,
                fingerprint=fingerprint,
                status="INFRASTRUCTURE_ERROR",
                error=error,
                state=state,
            )
        except Exception as error:  # noqa: BLE001 - 推进意外失败按 FAILED 记录
            return await self._failure_result(
                environment,
                case,
                repetition=repetition,
                fingerprint=fingerprint,
                status="FAILED",
                error=error,
                state=state,
            )
        finally:
            # 环境独立且不共享业务状态;teardown 清理防止失败用例残留的
            # QUEUED Run 被下一次恢复扫描捡起。捕获已完成,痕迹在结果里。
            await environment.aclose()
            if self._cleanup_environments:
                await environment.cleanup()

        return CaseResult(
            case_id=case.id,
            repetition=repetition,
            status="COMPLETED",
            included_in_quality_score=True,
            fingerprint=fingerprint,
            run_id=state.run_id,
            workspace_id=environment.workspace_id,
            events=capture.events,
            conversation_messages=capture.conversation_messages,
            tool_calls=capture.tool_calls,
            operations=capture.operations,
            final_state=capture.final_state,
        )

    # -- 内部:驱动与捕获 -------------------------------------------------

    async def _drive(
        self, environment: IsolatedEnvironment, case: Scenario, state: _DriveState
    ) -> None:
        """驱动一次完整确认链路;异常向上冒泡由 run_one 分类。"""
        application: ProductApplication = environment.application
        consumed_message_indexes = {
            interaction.message_index
            for interaction in case.interactions
            if interaction.message_index is not None
        }
        user_turns = [
            (index, message.content)
            for index, message in enumerate(case.messages)
            if message.role == "user" and index not in consumed_message_indexes
        ]
        if not user_turns:
            raise RuntimeError(f"scenario {case.id} has no user message to start a run")
        state.current_message_index = user_turns[0][0]
        view = await application.start(user_turns[0][1])
        state.run_id = view.run_id
        state.run_ids = [view.run_id]
        state.session_id = await self._session_id(view.run_id)
        interaction_index = 0
        view, interaction_index = await self._drive_current_run(
            application, environment, case, state, view, interaction_index
        )
        for message_index, user_message in user_turns[1:]:
            assert state.session_id is not None
            state.current_message_index = message_index
            view = await application.append_message(state.session_id, user_message)
            state.run_id = view.run_id
            assert state.run_ids is not None
            state.run_ids.append(view.run_id)
            view, interaction_index = await self._drive_current_run(
                application, environment, case, state, view, interaction_index
            )
        if interaction_index != len(case.interactions):
            raise RuntimeError(
                f"scenario {case.id} has {len(case.interactions) - interaction_index} "
                "unconsumed user interaction(s)"
            )

    async def _drive_current_run(
        self,
        application: ProductApplication,
        environment: IsolatedEnvironment,
        case: Scenario,
        state: _DriveState,
        view: Any,
        interaction_index: int,
    ) -> tuple[Any, int]:
        """仅恢复当前 Run 的显式补充信息/确认决定,直到真实终态。"""
        for _ in range(_MAX_DRIVE_STEPS):
            if view.status is RunStatus.WAITING_INPUT:
                assert view.pending_input_id is not None
                interaction = self._next_interaction(
                    case, interaction_index, "input", state.current_message_index
                )
                interaction_index += 1
                if interaction.message_index is not None:
                    state.current_message_index = interaction.message_index
                values = await self._input_values(
                    environment, view.pending_input_id, interaction.values
                )
                view = await application.answer(view.pending_input_id, values)
            elif view.status is RunStatus.WAITING_CONFIRMATION:
                assert view.pending_confirmation_id is not None
                interaction = self._next_interaction(
                    case,
                    interaction_index,
                    "confirmation",
                    state.current_message_index,
                )
                interaction_index += 1
                if interaction.message_index is not None:
                    state.current_message_index = interaction.message_index
                if interaction.decision == "accept":
                    view = await application.confirm(view.pending_confirmation_id)
                else:
                    view = await application.reject_confirmation(
                        view.pending_confirmation_id
                    )
            elif view.status is RunStatus.WAITING_RECONCILIATION:
                view = await application.recover(view.run_id)
            elif view.status is RunStatus.QUEUED:
                # 推进未完成(例如 NOT_APPLIED 重试)会把 Run 退回 QUEUED
                # 等下一轮推进;生产由执行器后台兜底循环捞取,评测里的同步
                # 门面不跑后台循环,所以按同一语义在这里再推一次。反复退回会
                # 耗尽 Attempt 预算,最终由 Runtime 自己判 FAILED。
                view = await application.resume(view.run_id)
            elif view.status is RunStatus.COMPLETED:
                return view, interaction_index
            elif view.status in _TERMINAL_RUN_STATUSES:
                if view.status is RunStatus.FAILED:
                    failure_code = await self._run_failure_code(
                        environment.workspace_id, view.run_id
                    )
                    if failure_code in _PROVIDER_FAILURE_CODES:
                        raise EvalProviderError(
                            "provider request failed with normalized code "
                            f"{failure_code}"
                        )
                if (
                    view.status is RunStatus.FAILED
                    and state.observed is not None
                    and state.observed.recorded
                ):
                    raise state.observed.recorded[0]
                raise RuntimeError(
                    f"scenario {case.id} ended in unsuccessful Run status "
                    f"{view.status.value}"
                )
            else:
                # 推进回调异常被执行器收尾(Run 回 QUEUED):
                # Backend 在流边界记录的真实异常在此重新抛出,按类型分类。
                if state.observed is not None and state.observed.recorded:
                    raise state.observed.recorded[0]
                raise RuntimeError(
                    f"case {case.id} stalled in non-terminal status "
                    f"{view.status.value}; Task 2 does not drive recovery paths"
                )
        raise RuntimeError(
            f"case {case.id} exceeded the drive step budget of {_MAX_DRIVE_STEPS}"
        )

    async def _run_failure_code(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> str | None:
        """从权威 Run 行读取失败码,区分 Provider 拒绝与 Agent 失败。"""
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            return await session.scalar(
                select(Run.failure_code).where(
                    Run.workspace_id == workspace_id, Run.id == run_id
                )
            )
        finally:
            await session.close()

    async def _session_id(self, run_id: uuid.UUID) -> uuid.UUID:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            value = await session.scalar(select(Run.session_id).where(Run.id == run_id))
            if value is None:
                raise RuntimeError(f"run {run_id} has no Session")
            return value
        finally:
            await session.close()

    @staticmethod
    def _next_interaction(
        case: Scenario,
        index: int,
        expected_kind: str,
        current_message_index: int | None,
    ) -> UserInteraction:
        if index >= len(case.interactions):
            raise RuntimeError(
                f"scenario {case.id} requires a {expected_kind} interaction but none was declared"
            )
        interaction = case.interactions[index]
        if interaction.kind != expected_kind:
            raise RuntimeError(
                f"scenario {case.id} expected interaction {interaction.kind!r}, "
                f"but Runtime requested {expected_kind!r}"
            )
        next_user_index = next(
            (
                message_index
                for message_index, message in enumerate(case.messages)
                if message.role == "user"
                and current_message_index is not None
                and message_index > current_message_index
            ),
            None,
        )
        if (
            interaction.message_index is not None
            and interaction.message_index != next_user_index
        ):
            raise RuntimeError(
                f"scenario {case.id} interaction at message_index="
                f"{interaction.message_index} is not the next user turn "
                f"({next_user_index!r})"
            )
        return interaction

    async def _input_values(
        self,
        environment: IsolatedEnvironment,
        input_request_id: uuid.UUID,
        declared_values: Mapping[str, str],
    ) -> dict[str, str]:
        """仅把场景显式声明的回答映射为 Fixture ID;从不猜测候选店铺。"""
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            request = (
                await session.scalars(
                    select(InputRequest).where(
                        InputRequest.workspace_id == environment.workspace_id,
                        InputRequest.id == input_request_id,
                    )
                )
            ).one_or_none()
            if request is None:
                raise RuntimeError(f"input request {input_request_id} vanished")
            sku: str | None = None
            if request.tool_call_id is not None:
                tool_call = (
                    await session.scalars(
                        select(ToolCall).where(
                            ToolCall.workspace_id == environment.workspace_id,
                            ToolCall.id == request.tool_call_id,
                        )
                    )
                ).one_or_none()
                if tool_call is not None:
                    arguments: Mapping[str, Any] = (
                        tool_call.normalized_arguments
                        if tool_call.normalized_arguments is not None
                        else tool_call.raw_arguments
                    )
                    raw_sku = dict(arguments).get("sku")
                    sku = str(raw_sku) if raw_sku is not None else None
            values: dict[str, str] = {}
            for field_path in request.field_paths:
                if field_path not in declared_values:
                    raise RuntimeError(
                        f"scenario has no explicit value for input field {field_path!r}"
                    )
                raw_value = declared_values[field_path]
                if field_path == "store_id":
                    store_id = environment.store_id_for_symbol(
                        raw_value, sku=sku
                    )
                    if store_id is None:
                        raise RuntimeError(
                            f"scenario references unseeded store {raw_value!r}"
                        )
                    values[field_path] = str(store_id)
                else:
                    values[field_path] = raw_value
            extra_values = set(declared_values) - set(request.field_paths)
            if extra_values:
                raise RuntimeError(
                    "scenario supplied undeclared input fields: "
                    + ", ".join(sorted(extra_values))
                )
            return values
        finally:
            await session.close()

    async def _capture(
        self, environment: IsolatedEnvironment, run_ids: Sequence[uuid.UUID]
    ) -> _Capture:
        """捕获 Run 的完整持久痕迹与权威最终状态。"""
        if not run_ids:
            return _Capture()
        run_ids_set = set(run_ids)
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            run_rows = (
                await session.scalars(
                    select(Run).where(
                        Run.workspace_id == environment.workspace_id,
                        Run.id.in_(run_ids_set),
                    )
                )
            ).all()
            session_ids = {run.session_id for run in run_rows}
            messages = (
                await session.scalars(
                    select(Message)
                    .where(
                        Message.workspace_id == environment.workspace_id,
                        Message.session_id.in_(session_ids),
                    )
                    .order_by(Message.sequence_no)
                )
            ).all()
            events = (
                await session.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.workspace_id == environment.workspace_id,
                        AgentEvent.run_id.in_(run_ids_set),
                    )
                    .order_by(AgentEvent.sequence_no)
                )
            ).all()
            tool_rows = (
                await session.execute(
                    select(ToolCall, ToolDefinition.stable_key, Message.sequence_no)
                .join(ToolVersion, ToolCall.tool_version_id == ToolVersion.id)
                .join(ToolDefinition, ToolVersion.tool_id == ToolDefinition.id)
                .join(Message, Message.id == ToolCall.message_id)
                    .where(
                        ToolCall.workspace_id == environment.workspace_id,
                        ToolCall.run_id.in_(run_ids_set),
                    )
                    .order_by(Message.sequence_no)
                )
            ).all()
            operations = (
                await session.scalars(
                    select(Operation)
                    .join(ToolCall, Operation.tool_call_id == ToolCall.id)
                    .where(
                        Operation.workspace_id == environment.workspace_id,
                        ToolCall.run_id.in_(run_ids_set),
                    )
                )
            ).all()
        finally:
            await session.close()

        operation_captures: list[OperationCapture] = []
        logical_writes = 0
        for operation in operations:
            writes = await environment.commerce_write_observer.count_logical_writes(
                str(operation.operation_id)
            )
            logical_writes += writes
            operation_captures.append(
                OperationCapture(
                    status=operation.status.value,
                    adapter_key=operation.adapter_key,
                    logical_writes=writes,
                )
            )
        return _Capture(
            events=tuple(
                EventCapture(
                    sequence_no=event.sequence_no,
                    event_type=event.event_type,
                    payload=dict(event.payload),
                )
                for event in events
            ),
            conversation_messages=tuple(
                MessageCapture(
                    run_id=message.run_id,
                    sequence_no=message.sequence_no,
                    role=message.role.value,
                    content=message.content,
                )
                for message in messages
                if message.role.value in {"USER", "ASSISTANT", "TOOL"}
            ),
            tool_calls=tuple(
                ToolCallCapture(
                    stable_key=stable_key,
                    ordinal=tool_call.ordinal,
                    sequence_no=message_sequence,
                    status=tool_call.status.value,
                    failure_code=tool_call.failure_code,
                    normalized_arguments=(
                        _symbolize_tool_arguments(
                            environment, tool_call.normalized_arguments
                        )
                        if tool_call.normalized_arguments is not None
                        else None
                    ),
                )
                for tool_call, stable_key, message_sequence in tool_rows
            ),
            operations=tuple(operation_captures),
            final_state=await environment.capture_final_state(
                logical_writes=logical_writes
            ),
        )
    # -- 内部:结果构造 ---------------------------------------------------

    def _skipped_result(self, case: Scenario, *, repetition: int) -> CaseResult:
        return CaseResult(
            case_id=case.id,
            repetition=repetition,
            status="SKIPPED",
            included_in_quality_score=False,
            fingerprint=protocol_fingerprint(case, self.config, self.manifest),
        )

    def _result_of_failure(
        self,
        case: Scenario,
        *,
        repetition: int,
        fingerprint: str,
        status: CaseStatus,
        error: BaseException,
    ) -> CaseResult:
        """环境创建失败的结果(尚无 Run/Workspace 可捕获)。"""
        return CaseResult(
            case_id=case.id,
            repetition=repetition,
            status=status,
            included_in_quality_score=False,
            fingerprint=fingerprint,
            error=_describe(error),
        )

    async def _failure_result(
        self,
        environment: IsolatedEnvironment,
        case: Scenario,
        *,
        repetition: int,
        fingerprint: str,
        status: CaseStatus,
        error: BaseException,
        state: _DriveState,
    ) -> CaseResult:
        """推进失败的结果:best-effort 捕获已产生的持久痕迹。"""
        try:
            capture = await self._capture(environment, state.run_ids or ())
        except Exception:  # noqa: BLE001 - 失败路径的捕获本身不得掩盖原错误
            capture = _Capture()
        return CaseResult(
            case_id=case.id,
            repetition=repetition,
            status=status,
            included_in_quality_score=(status == "FAILED"),
            fingerprint=fingerprint,
            run_id=state.run_id,
            workspace_id=environment.workspace_id,
            error=_describe(error),
            events=capture.events,
            conversation_messages=capture.conversation_messages,
            tool_calls=capture.tool_calls,
            operations=capture.operations,
            final_state=capture.final_state,
        )


__all__ = [
    "EvalProviderError",
    "EvalRunner",
]
