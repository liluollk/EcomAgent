"""Eval Runner(计划 05 Task 2 Step 3)。

:class:`EvalRunner` 按 case/repetition 逐个执行 Scenario:每例经
:class:`eval.fixtures.IsolatedEnvironment` 组装独立权威环境,以注入的
Backend 驱动真实 RuntimeHost 推进(start → 缺店铺 Input → 确认 → 终态),
捕获完整 AgentEvent/ToolCall/Operation 与权威最终状态到
:class:`eval.results.CaseResult`。判分(任务成败)不属 Runner,由 Scorer
(Task 3)消费捕获结果。

确定性分类规则(不猜测):

- ``INFRASTRUCTURE_ERROR``:注入 Backend 抛 :class:`EvalProviderError`
  (如 401)、连接失败(``OSError`` 系,含 ``ConnectionError``/``TimeoutError``)
  或依赖不可用(``sqlalchemy`` ``InterfaceError``/``OperationalError``)——
  这类失败与被测 Agent 行为无关,``included_in_quality_score=False``。
  RunJobWorker 会吞掉推进异常并把 Job 释放回 PENDING(Run 回 QUEUED),
  因此 Backend 异常经观察代理(``_ObservingBackend``)在流边界记录、于
  Run 停滞时原样重抛,按真实异常类型分类;
- ``FAILED``:推进中的其他意外异常(同样不计入质量分母——无完整可信轨迹);
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

from adapters.commerce.product_runtime import ProductRuntime
from eval.config import EvalConfig
from eval.fixtures import IsolatedEnvironment
from eval.results import (
    CaseResult,
    CaseStatus,
    EvalRoundResult,
    EventCapture,
    FinalState,
    OperationCapture,
    ToolCallCapture,
)
from eval.schema import Scenario, protocol_fingerprint
from runtime.backend.protocol import (
    AgentBackend,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)
from runtime.interfaces.state import RunStatus
from runtime.persistence.models.configuration import ToolDefinition, ToolVersion
from runtime.persistence.models.events import AgentEvent
from runtime.persistence.models.execution import Operation, ToolCall
from runtime.persistence.models.interaction import InputRequest


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

_MAX_DRIVE_STEPS: Final = 12
"""单 case 驱动步数上限(input/confirm 恢复推进次数的确定性上界)。"""


@dataclass
class _DriveState:
    """一次驱动的可观测状态(异常路径下仍可 best-effort 捕获)。"""

    run_id: uuid.UUID | None = None
    observed: _ObservingBackend | None = None


class _ObservingBackend:
    """Backend 代理:记录 ``stream`` 抛出的异常并原样续传事件。

    必要性:RunJobWorker 会吞掉推进回调的异常并把 Job 释放回 PENDING
    (Run 回 QUEUED),Provider 失败的原始异常因此不会传到驱动方。本代理
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
    tool_calls: tuple[ToolCallCapture, ...] = ()
    operations: tuple[OperationCapture, ...] = ()
    final_state: FinalState | None = None


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


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
        backend_factory: Callable[[Scenario], AgentBackend],
        repetitions: int | None = None,
        cleanup_environments: bool = True,
    ) -> None:
        self._engine = engine
        self.config = config
        """固定运行配置(协议指纹的配置分量;只读暴露)。"""
        self._backend_factory = backend_factory
        self._repetitions = config.repetitions if repetitions is None else repetitions
        self._cleanup_environments = cleanup_environments

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
        fingerprint = protocol_fingerprint(case, self.config)
        resolved_backend = backend if backend is not None else self._backend_factory(case)
        observing = _ObservingBackend(resolved_backend)
        state = _DriveState(observed=observing)
        try:
            environment = await IsolatedEnvironment.create(
                self._engine, case, backend=observing
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
            capture = await self._capture(environment, state.run_id)
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
            # 环境独立且不共享业务状态;teardown 清理防止失败的 ADVANCE_RUN
            # Job(回滚释放回 PENDING)被后续 case 的 Worker 推进误领取
            # (LeaseService.claim 不区分 workspace)。捕获已完成,痕迹在结果里。
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
            tool_calls=capture.tool_calls,
            operations=capture.operations,
            final_state=capture.final_state,
        )

    # -- 内部:驱动与捕获 -------------------------------------------------

    async def _drive(
        self, environment: IsolatedEnvironment, case: Scenario, state: _DriveState
    ) -> None:
        """驱动一次完整确认链路;异常向上冒泡由 run_one 分类。"""
        runtime: ProductRuntime = environment.runtime
        first_user = next(
            (message.content for message in case.messages if message.role == "user"),
            None,
        )
        if first_user is None:
            raise RuntimeError(f"scenario {case.id} has no user message to start a run")
        view = await runtime.start(first_user)
        state.run_id = view.run_id
        for _ in range(_MAX_DRIVE_STEPS):
            if view.status is RunStatus.WAITING_INPUT:
                assert view.pending_input_id is not None
                values = await self._input_values(environment, view.pending_input_id)
                view = await runtime.answer(view.pending_input_id, values)
            elif view.status is RunStatus.WAITING_CONFIRMATION:
                assert view.pending_confirmation_id is not None
                view = await runtime.confirm(view.pending_confirmation_id)
            elif view.status in _TERMINAL_RUN_STATUSES:
                return
            else:
                # 推进回调异常被 RunJobWorker 吞掉并释放 Job(Run 回 QUEUED):
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

    async def _input_values(
        self, environment: IsolatedEnvironment, input_request_id: uuid.UUID
    ) -> dict[str, str]:
        """为挂起 Input Request 构造确定性回答(店铺经种子符号映射)。"""
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
                if field_path != "store_id":
                    raise RuntimeError(
                        f"unsupported input field {field_path!r} for scripted eval drive"
                    )
                store_id = (
                    environment.store_id_for_sku(sku)
                    if sku is not None
                    else environment.single_store_id
                )
                if store_id is None:
                    raise RuntimeError(
                        f"no seeded store to answer field {field_path!r}"
                        + (f" for sku {sku!r}" if sku is not None else "")
                    )
                values[field_path] = str(store_id)
            return values
        finally:
            await session.close()

    async def _capture(
        self, environment: IsolatedEnvironment, run_id: uuid.UUID | None
    ) -> _Capture:
        """捕获 Run 的完整持久痕迹与权威最终状态。"""
        if run_id is None:
            return _Capture()
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            events = (
                await session.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.workspace_id == environment.workspace_id,
                        AgentEvent.run_id == run_id,
                    )
                    .order_by(AgentEvent.sequence_no)
                )
            ).all()
            tool_rows = (
                await session.execute(
                    select(ToolCall, ToolDefinition.stable_key)
                    .join(ToolVersion, ToolCall.tool_version_id == ToolVersion.id)
                    .join(ToolDefinition, ToolVersion.tool_id == ToolDefinition.id)
                    .where(
                        ToolCall.workspace_id == environment.workspace_id,
                        ToolCall.run_id == run_id,
                    )
                    .order_by(ToolCall.ordinal)
                )
            ).all()
            operations = (
                await session.scalars(
                    select(Operation)
                    .join(ToolCall, Operation.tool_call_id == ToolCall.id)
                    .where(
                        Operation.workspace_id == environment.workspace_id,
                        ToolCall.run_id == run_id,
                    )
                )
            ).all()
        finally:
            await session.close()

        operation_captures: list[OperationCapture] = []
        logical_writes = 0
        for operation in operations:
            writes = await environment.runtime.adapter.count_logical_writes(
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
            tool_calls=tuple(
                ToolCallCapture(
                    stable_key=stable_key,
                    ordinal=tool_call.ordinal,
                    status=tool_call.status.value,
                    failure_code=tool_call.failure_code,
                    normalized_arguments=(
                        dict(tool_call.normalized_arguments)
                        if tool_call.normalized_arguments is not None
                        else None
                    ),
                )
                for tool_call, stable_key in tool_rows
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
            fingerprint=protocol_fingerprint(case, self.config),
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
            capture = await self._capture(environment, state.run_id)
        except Exception:  # noqa: BLE001 - 失败路径的捕获本身不得掩盖原错误
            capture = _Capture()
        return CaseResult(
            case_id=case.id,
            repetition=repetition,
            status=status,
            included_in_quality_score=False,
            fingerprint=fingerprint,
            run_id=state.run_id,
            workspace_id=environment.workspace_id,
            error=_describe(error),
            events=capture.events,
            tool_calls=capture.tool_calls,
            operations=capture.operations,
            final_state=capture.final_state,
        )


__all__ = [
    "EvalProviderError",
    "EvalRunner",
]
