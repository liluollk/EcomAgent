"""AgentLoop 组装、顺序执行、挂起恢复与预算触发测试(计划 02 Task 9)。

- Run 状态迁移断言走真库(沿用 tests/runtime 夹具:同一事务内 flush,结束回滚);
- 模型流用按轮次回放的 SequencedBackend;PreToolUse 管线与 ToolHandler 用
  确定性 fake;InputService/ConfirmationService/ReconciliationService 用真实
  服务验证持久化语义;
- Runtime 测试按 jobs 测试模式使用已提交种子行 + 独立 Session 工厂 + 清理。
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendFailed,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.executor import AdvanceOutcome, RunClaim, RunExecutor
from runtime.execution.pretool.decisions import (
    POLICY_DENIED,
    PreToolDecision,
    SchemaDiagnostic,
    allow,
    deny,
    require_confirmation,
    require_input,
)
from runtime.execution.pretool.pipeline import ToolCallRequest
from runtime.execution.reconciliation import ReconcileResult, ReconciliationService
from runtime.execution.run_service import RunService
from runtime.execution.tool_executor import PreparedToolWrite, PreparedWriteRejection
from runtime.execution.tool_result_processor import (
    ToolExecutionReceipt,
    ToolResultProcessor,
)
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interaction.input_service import InputService
from runtime.interfaces.requests import ConfirmationSpec, InputRequirement
from runtime.interfaces.state import RunStatus
from runtime.orchestration.agent_loop import AgentLoop, ToolPin
from runtime.orchestration.context import ContextAssembler
from runtime.orchestration.limits import RunLimits
from runtime.orchestration.runtime import Runtime
from runtime.orchestration.summarization import ConversationSummarizer
from runtime.persistence.base import (
    ConfigScope,
    MembershipRole,
    MessageRole,
    MessageStatus,
    ModelBackendType,
    RunNextStep,
    ToolCallStatus,
    ToolEffect,
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
from runtime.persistence.models.execution import (
    Operation,
    Run,
    RunAttempt,
    ToolCall,
)
from runtime.persistence.models.identity import (
    User,
    Workspace,
    WorkspaceMembership,
)
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
)
from runtime.persistence.repositories.events import EventRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


# ---------------------------------------------------------------------------
# 测试常量与确定性替身
# ---------------------------------------------------------------------------

TEST_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}

TEST_LIMITS = RunLimits(
    max_model_rounds=4,
    max_tool_calls=5,
    tool_timeout_seconds=60,
    run_deadline_seconds=3600,
    backend_retries=1,
    reconcile_budget=3,
)

READ_TOOL_KEY = "read_tool"
WRITE_TOOL_KEY = "write_tool"

DEFAULT_WRITE_EXECUTION_PROFILE = ToolExecutionProfile(adapter_key="local")
"""直接构造 AgentLoop 的默认写执行 Profile(历史上是 Loop 的全局默认值)。"""


@dataclass
class FakeClock:
    """可手动推进的确定性时钟。"""

    moment: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def now(self) -> datetime:
        return self.moment

    def advance(self, *, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class LengthTokenEstimator:
    """确定性估算:1 字符 = 1 token。"""

    def estimate(self, text: str) -> int:
        return len(text)


class SilentSummaryBackend:
    """Summary 用确定性 Backend(测试不触达压缩路径)。"""

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(capability_keys=frozenset({"streaming"}))

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        yield BackendCompleted(finish_reason="stop")


def make_assembler() -> ContextAssembler:
    estimator = LengthTokenEstimator()
    return ContextAssembler(
        safety_rules="runtime safety rules for agent loop tests",
        agent_instructions="you are the test agent",
        workspace_summary="workspace summary",
        summarizer=ConversationSummarizer(
            backend=SilentSummaryBackend(),
            estimator=estimator,
            input_budget_tokens=100_000,
        ),
        estimator=estimator,
    )


class ScriptedPipeline:
    """按 evaluate 顺序回放 PreTool 决策的确定性管线替身。"""

    def __init__(self, decisions: Sequence[PreToolDecision]) -> None:
        self._decisions = list(decisions)
        self.evaluated: list[ToolCallRequest] = []

    async def evaluate(self, tool_call: ToolCallRequest) -> PreToolDecision:
        self.evaluated.append(tool_call)
        if self._decisions:
            return self._decisions.pop(0)
        return allow()


class SequencedBackend:
    """按轮次回放事件脚本:每次 stream 消耗一个脚本,耗尽后返回空完成。"""

    def __init__(self, rounds: Sequence[Sequence[BackendEvent]]) -> None:
        self.rounds: list[list[BackendEvent]] = [list(round) for round in rounds]
        self.stream_calls = 0
        self.requests: list[BackendRequest] = []

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        self.stream_calls += 1
        self.requests.append(request)
        events = (
            self.rounds.pop(0) if self.rounds else [BackendCompleted(finish_reason="stop")]
        )
        for event in events:
            if cancellation.is_cancelled:
                return
            yield event


class FakeReadHandler:
    """读 Tool 执行替身;可注入超时(推进时钟)。"""

    def __init__(
        self,
        clock: FakeClock | None = None,
        *,
        status: str = "APPLIED",
        payload: Any = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.clock = clock
        self.status = status
        self.payload = payload
        self.timeout_seconds = timeout_seconds
        self.calls: list[dict[str, Any]] = []

    async def read(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        deadline: datetime,
    ) -> ToolExecutionReceipt:
        self.calls.append(
            {
                "tool_call_id": tool_call_id,
                "normalized_arguments": dict(normalized_arguments),
                "deadline": deadline,
            }
        )
        if self.clock is not None and self.timeout_seconds is not None:
            self.clock.advance(seconds=self.timeout_seconds)
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id, status=self.status, payload=self.payload
        )


class FakeWriteHandler:
    """写 Tool 执行替身。

    ``fail=True`` 模拟响应丢失(UNKNOWN);``rejection`` 模拟 prepare 阶段的确定性
    拒绝;``execution_status``/``execution_retryable``/``execution_error_code``
    模拟适配器回执及其显式可重试声明与诊断码。
    """

    def __init__(
        self,
        *,
        fail: bool = False,
        rejection: PreparedWriteRejection | None = None,
        execution_status: str = "APPLIED",
        execution_retryable: bool = False,
        execution_error_code: str | None = None,
    ) -> None:
        self.fail = fail
        self.rejection = rejection
        self.execution_status = execution_status
        self.execution_retryable = execution_retryable
        self.execution_error_code = execution_error_code
        self.calls: list[dict[str, Any]] = []
        self.prepare_calls = 0
        self.expected_state: dict[str, Any] = {"operation_type": "TEST_WRITE"}

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        self.prepare_calls += 1
        if self.rejection is not None:
            return PreparedToolWrite(
                command=None,
                expected_state=None,
                request_fingerprint="rejected-fingerprint",
                rejection=self.rejection,
            )
        return PreparedToolWrite(
            command={"arguments": dict(normalized_arguments)},
            expected_state=dict(self.expected_state),
            request_fingerprint="fake-write-fingerprint",
        )

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        self.calls.append(
            {
                "tool_call_id": tool_call_id,
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "normalized_arguments": dict(prepared.command["arguments"]),
            }
        )
        if self.fail:
            raise RuntimeError("connection lost")
        applied = self.execution_status == "APPLIED"
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            status=self.execution_status,
            payload={"result": "written"} if applied else None,
            external_reference="ext-1" if applied else None,
            error_code=self.execution_error_code,
            retryable=self.execution_retryable,
        )


@dataclass(frozen=True)
class FakeToolPins:
    """内存 ToolPin 解析器(真实实现由 Runtime 从库装配)。"""

    pins: Mapping[str, ToolPin]

    def by_stable_key(self, stable_key: str) -> ToolPin | None:
        return self.pins.get(stable_key)

    def by_version_id(self, version_id: uuid.UUID) -> ToolPin | None:
        for pin in self.pins.values():
            if pin.version_id == version_id:
                return pin
        return None


def _tool_rows(stable_key: str, effect: ToolEffect) -> tuple[ToolDefinition, ToolVersion]:
    definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        workspace_id=None,
        stable_key=stable_key,
        name=stable_key,
    )
    version = ToolVersion(
        id=uuid.uuid4(),
        workspace_id=None,
        tool_id=definition.id,
        version=1,
        effect=effect,
        input_schema={"type": "object"},
        output_schema=TEST_OUTPUT_SCHEMA,
        schema_hash="schema-hash-1",
    )
    return definition, version


async def _seed_assistant_messages(
    db_session: AsyncSession, run: Run, count: int, *, start_sequence: int = 1
) -> None:
    for offset in range(count):
        db_session.add(
            Message(
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                run_id=run.id,
                role=MessageRole.ASSISTANT,
                status=MessageStatus.COMPLETED,
                content=f"assistant round {start_sequence + offset}",
                sequence_no=start_sequence + offset,
            )
        )
    await db_session.flush()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def tool_pins(db_session: AsyncSession) -> FakeToolPins:
    read_def, read_ver = _tool_rows(READ_TOOL_KEY, ToolEffect.READ)
    write_def, write_ver = _tool_rows(WRITE_TOOL_KEY, ToolEffect.WRITE)
    db_session.add_all([read_def, read_ver, write_def, write_ver])
    return FakeToolPins(
        pins={
            READ_TOOL_KEY: ToolPin(
                stable_key=READ_TOOL_KEY,
                version=1,
                version_id=read_ver.id,
                effect=ToolEffect.READ,
            ),
            WRITE_TOOL_KEY: ToolPin(
                stable_key=WRITE_TOOL_KEY,
                version=1,
                version_id=write_ver.id,
                effect=ToolEffect.WRITE,
            ),
        }
    )


@pytest.fixture()
def make_agent_loop(
    db_session: AsyncSession,
    seeded_workspace,
    tool_pins: FakeToolPins,
    fake_clock: FakeClock,
):
    """AgentLoop 工厂:默认绑定种子 workspace、测试预算与确定性时钟。"""

    def _make(
        *,
        backend,
        pipeline,
        read_handler: FakeReadHandler | None = None,
        write_handler: FakeWriteHandler | None = None,
        tool_pins_override: FakeToolPins | None = None,
        limits: RunLimits = TEST_LIMITS,
        clock: FakeClock | None = None,
    ) -> AgentLoop:
        selected_clock = clock if clock is not None else fake_clock
        selected_pins = tool_pins_override
        if selected_pins is None:
            selected_pins = FakeToolPins(
                pins={
                    key: replace(
                        pin,
                        handler=(
                            read_handler
                            if pin.effect is ToolEffect.READ
                            else write_handler
                        ),
                        output_schema=TEST_OUTPUT_SCHEMA,
                        execution_profile=(
                            None
                            if pin.effect is ToolEffect.READ
                            else DEFAULT_WRITE_EXECUTION_PROFILE
                        ),
                    )
                    for key, pin in tool_pins.pins.items()
                }
            )
        return AgentLoop(
            db_session,
            workspace_id=seeded_workspace.workspace.id,
            backend=backend,
            assembler=make_assembler(),
            limits=limits,
            pipeline=pipeline,
            tool_pins=selected_pins,
            processor=ToolResultProcessor(),
            clock=selected_clock.now,
        )

    return _make


@pytest.fixture()
def run_with_two_tool_calls(seeded_run: Run) -> SimpleNamespace:
    """脚本化一轮输出两个 ToolCall 的 Run(计划 Step 1 场景)。"""
    backend = SequencedBackend(
        [
            [
                ToolCallCompleted(
                    call_id="c0", name=READ_TOOL_KEY, arguments={"listing_id": "L1"}
                ),
                ToolCallCompleted(
                    call_id="c1", name=READ_TOOL_KEY, arguments={"listing_id": "L2"}
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ]
        ]
    )
    return SimpleNamespace(run=seeded_run, backend=backend)


@pytest.fixture()
def run_service(db_session: AsyncSession, seeded_workspace) -> RunService:
    return RunService(session=db_session, workspace_id=seeded_workspace.workspace.id)


async def _load_tool_calls(db_session: AsyncSession, run: Run) -> list[ToolCall]:
    stmt = (
        select(ToolCall)
        .where(ToolCall.workspace_id == run.workspace_id, ToolCall.run_id == run.id)
        .order_by(ToolCall.ordinal)
        .execution_options(populate_existing=True)
    )
    return list((await db_session.scalars(stmt)).all())


async def _load_run(db_session: AsyncSession, run: Run) -> Run:
    fetched = await RunRepository(db_session).get(run.workspace_id, run.id)
    assert fetched is not None
    return fetched


def _require_input_decision() -> PreToolDecision:
    return require_input(
        input_requirement=InputRequirement(
            field_paths=("store_id",),
            question="Which store?",
            options={"store_id": ({"value": "s-1", "label": "旗舰店"},)},
        ),
        schema_diagnostic=SchemaDiagnostic(
            field_paths=("store_id",), message="store id missing"
        ),
    )


def _require_input_decision_without_options() -> PreToolDecision:
    """无服务端候选项的 Input Request(纯 Schema 缺失场景)。"""
    return require_input(
        input_requirement=InputRequirement(
            field_paths=("store_id",), question="Which store?"
        ),
        schema_diagnostic=SchemaDiagnostic(
            field_paths=("store_id",), message="store id missing"
        ),
    )


# ---------------------------------------------------------------------------
# 计划原文两个必含测试
# ---------------------------------------------------------------------------


async def test_multiple_tool_calls_stop_at_first_waiting_input(
    make_agent_loop, run_with_two_tool_calls, db_session: AsyncSession
) -> None:
    scenario = run_with_two_tool_calls
    loop = make_agent_loop(
        backend=scenario.backend,
        pipeline=ScriptedPipeline([_require_input_decision()]),
    )

    result = await loop.advance(scenario.run.id)

    assert result.persisted_ordinals == [0, 1]
    assert result.processed_ordinals == [0]
    assert result.run_status == RunStatus.WAITING_INPUT

    # Run 状态真库断言。
    persisted = await _load_run(db_session, scenario.run)
    assert persisted.status is PersistenceRunStatus.WAITING_INPUT

    calls = await _load_tool_calls(db_session, scenario.run)
    assert [call.ordinal for call in calls] == [0, 1]
    assert calls[0].status is ToolCallStatus.WAITING_INPUT
    assert calls[1].status is ToolCallStatus.PROPOSED


async def _load_input_requests(
    db_session: AsyncSession, run: Run
) -> list[InputRequest]:
    stmt = (
        select(InputRequest)
        .where(InputRequest.workspace_id == run.workspace_id)
        .order_by(InputRequest.created_at, InputRequest.id)
        .execution_options(populate_existing=True)
    )
    return list((await db_session.scalars(stmt)).all())


async def test_waiting_input_persists_server_options(
    make_agent_loop, run_with_two_tool_calls, db_session: AsyncSession
) -> None:
    """REQUIRE_INPUT 的服务端候选项随 Request 持久化。

    Workbench/API 直接读取持久化的候选项,不再各自按领域规则重算;否则位置这类
    新候选永远无法呈现给用户。
    """
    scenario = run_with_two_tool_calls
    loop = make_agent_loop(
        backend=scenario.backend,
        pipeline=ScriptedPipeline([_require_input_decision()]),
    )

    result = await loop.advance(scenario.run.id)
    assert result.run_status == RunStatus.WAITING_INPUT

    requests = await _load_input_requests(db_session, scenario.run)
    assert len(requests) == 1
    stored = requests[0].options_json
    assert stored is not None
    assert [option["value"] for option in stored["store_id"]] == ["s-1"]
    assert [option["label"] for option in stored["store_id"]] == ["旗舰店"]


async def test_waiting_input_without_options_persists_none(
    make_agent_loop, seeded_run: Run, db_session: AsyncSession
) -> None:
    """无候选项时持久化为 NULL,而不是空字典——保持"服务端未给候选"的语义。"""
    backend = SequencedBackend(
        [
            [
                ToolCallCompleted(
                    call_id="c0", name=READ_TOOL_KEY, arguments={"listing_id": "L1"}
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ]
        ]
    )
    loop = make_agent_loop(
        backend=backend,
        pipeline=ScriptedPipeline([_require_input_decision_without_options()]),
    )

    await loop.advance(seeded_run.id)

    requests = await _load_input_requests(db_session, seeded_run)
    assert len(requests) == 1
    assert requests[0].options_json is None


async def test_loop_limit_fails_deterministically(
    db_session: AsyncSession,
    seeded_run: Run,
    make_agent_loop,
) -> None:
    run = seeded_run
    await _seed_assistant_messages(db_session, run, TEST_LIMITS.max_model_rounds)
    backend = SequencedBackend([[BackendCompleted(finish_reason="stop")]])

    first = await make_agent_loop(backend=backend, pipeline=ScriptedPipeline([])).advance(
        run.id
    )
    assert first.failure_code == "LOOP_LIMIT_EXCEEDED"
    assert first.run_status == RunStatus.FAILED

    persisted = await _load_run(db_session, run)
    assert persisted.failure_code == "LOOP_LIMIT_EXCEEDED"

    # 确定性:重入同一 Run 结论一致(终态不重放)。
    again = await make_agent_loop(
        backend=SequencedBackend([[BackendCompleted(finish_reason="stop")]]),
        pipeline=ScriptedPipeline([]),
    ).advance(run.id)
    assert again.failure_code == "LOOP_LIMIT_EXCEEDED"
    assert again.run_status == RunStatus.FAILED


# ---------------------------------------------------------------------------
# 正常完成与 DENY
# ---------------------------------------------------------------------------


async def test_loop_completes_without_tool_calls(
    make_agent_loop, seeded_run: Run, db_session: AsyncSession
) -> None:
    loop = make_agent_loop(
        backend=SequencedBackend(
            [[TextDelta(text_delta="final answer"), BackendCompleted(finish_reason="stop")]]
        ),
        pipeline=ScriptedPipeline([]),
    )
    result = await loop.advance(seeded_run.id)

    assert result.run_status is RunStatus.COMPLETED
    assert result.failure_code is None
    assert result.persisted_ordinals == []
    assert result.processed_ordinals == []

    persisted = await _load_run(db_session, seeded_run)
    assert persisted.status is PersistenceRunStatus.COMPLETED
    assert persisted.completed_at is not None
    messages = (
        await db_session.scalars(
            select(Message).where(
                Message.workspace_id == seeded_run.workspace_id,
                Message.run_id == seeded_run.id,
            )
        )
    ).all()
    assert [message.status for message in messages] == [MessageStatus.COMPLETED]


async def test_tool_results_are_replayed_with_their_assistant_tool_call(
    make_agent_loop, seeded_run: Run
) -> None:
    backend = SequencedBackend(
        [
            [
                ToolCallCompleted(
                    call_id="provider-call-1",
                    name=READ_TOOL_KEY,
                    arguments={"listing_id": "L1"},
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [BackendCompleted(finish_reason="stop")],
        ]
    )
    loop = make_agent_loop(
        backend=backend,
        pipeline=ScriptedPipeline([allow()]),
        read_handler=FakeReadHandler(payload={"listing_id": "L1", "price": "10"}),
    )

    result = await loop.advance(seeded_run.id)

    assert result.run_status is RunStatus.COMPLETED
    assert len(backend.requests) == 2
    second_messages = backend.requests[1].messages
    assistant_message = next(
        message for message in second_messages if message.role == "assistant"
    )
    tool_message = next(message for message in second_messages if message.role == "tool")
    assert len(assistant_message.tool_calls) == 1
    tool_call = assistant_message.tool_calls[0]
    assert tool_call.id == tool_message.call_id
    assert tool_call.name == READ_TOOL_KEY
    assert tool_call.arguments == {"listing_id": "L1"}


async def test_each_read_tool_uses_its_own_bound_handler(
    db_session: AsyncSession,
    seeded_run: Run,
    make_agent_loop,
) -> None:
    first_tool_key = "bound_read_tool_one"
    second_tool_key = "bound_read_tool_two"
    first_definition, first_version = _tool_rows(first_tool_key, ToolEffect.READ)
    second_definition, second_version = _tool_rows(second_tool_key, ToolEffect.READ)
    db_session.add_all([first_definition, first_version, second_definition, second_version])
    first_handler = FakeReadHandler(payload={"result": "first"})
    second_handler = FakeReadHandler(payload={"result": "second"})
    pins = FakeToolPins(
        pins={
            first_tool_key: ToolPin(
                stable_key=first_tool_key,
                version=1,
                version_id=first_version.id,
                effect=ToolEffect.READ,
                handler=first_handler,
                output_schema=TEST_OUTPUT_SCHEMA,
            ),
            second_tool_key: ToolPin(
                stable_key=second_tool_key,
                version=1,
                version_id=second_version.id,
                effect=ToolEffect.READ,
                handler=second_handler,
                output_schema=TEST_OUTPUT_SCHEMA,
            ),
        }
    )
    backend = SequencedBackend(
        [
            [
                ToolCallCompleted(call_id="first", name=first_tool_key, arguments={}),
                ToolCallCompleted(call_id="second", name=second_tool_key, arguments={}),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [BackendCompleted(finish_reason="stop")],
        ]
    )

    result = await make_agent_loop(
        backend=backend,
        pipeline=ScriptedPipeline([allow(), allow()]),
        tool_pins_override=pins,
    ).advance(seeded_run.id)

    assert result.run_status is RunStatus.COMPLETED
    persisted_calls = await _load_tool_calls(db_session, seeded_run)
    assert len(first_handler.calls) == len(second_handler.calls) == 1
    assert first_handler.calls[0]["tool_call_id"] == persisted_calls[0].id
    assert second_handler.calls[0]["tool_call_id"] == persisted_calls[1].id


async def test_write_operations_use_each_pin_execution_profile(
    db_session: AsyncSession,
    seeded_run: Run,
    make_agent_loop,
) -> None:
    """两个写 Tool 各自携带执行 Profile:Operation 的 Adapter 能力来自本 Tool。"""
    pricing_key = "pricing_write_tool"
    inventory_key = "inventory_write_tool"
    pricing_definition, pricing_version = _tool_rows(pricing_key, ToolEffect.WRITE)
    inventory_definition, inventory_version = _tool_rows(inventory_key, ToolEffect.WRITE)
    db_session.add_all(
        [pricing_definition, pricing_version, inventory_definition, inventory_version]
    )
    pricing_handler = FakeWriteHandler()
    inventory_handler = FakeWriteHandler()
    pins = FakeToolPins(
        pins={
            pricing_key: ToolPin(
                stable_key=pricing_key,
                version=1,
                version_id=pricing_version.id,
                effect=ToolEffect.WRITE,
                handler=pricing_handler,
                output_schema=TEST_OUTPUT_SCHEMA,
                execution_profile=ToolExecutionProfile(
                    adapter_key="pricing-mock",
                    supports_native_idempotency=False,
                    supports_reconciliation=True,
                ),
            ),
            inventory_key: ToolPin(
                stable_key=inventory_key,
                version=1,
                version_id=inventory_version.id,
                effect=ToolEffect.WRITE,
                handler=inventory_handler,
                output_schema=TEST_OUTPUT_SCHEMA,
                execution_profile=ToolExecutionProfile(
                    adapter_key="inventory-rest",
                    supports_native_idempotency=True,
                    supports_reconciliation=True,
                ),
            ),
        }
    )
    backend = SequencedBackend(
        [
            [
                ToolCallCompleted(
                    call_id="c0", name=pricing_key, arguments={"listing_id": "L1"}
                ),
                ToolCallCompleted(
                    call_id="c1", name=inventory_key, arguments={"listing_id": "L2"}
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [BackendCompleted(finish_reason="stop")],
        ]
    )

    result = await make_agent_loop(
        backend=backend,
        pipeline=ScriptedPipeline([allow(), allow()]),
        tool_pins_override=pins,
    ).advance(seeded_run.id)

    assert result.run_status is RunStatus.COMPLETED
    operations = list(
        (
            await db_session.scalars(
                select(Operation)
                .where(Operation.workspace_id == seeded_run.workspace_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    by_adapter = {operation.adapter_key: operation for operation in operations}
    assert set(by_adapter) == {"pricing-mock", "inventory-rest"}
    assert by_adapter["pricing-mock"].supports_native_idempotency is False
    assert by_adapter["pricing-mock"].supports_reconciliation is True
    assert by_adapter["inventory-rest"].supports_native_idempotency is True
    assert by_adapter["inventory-rest"].supports_reconciliation is True


async def test_missing_write_execution_profile_fails_deterministically(
    db_session: AsyncSession,
    seeded_run: Run,
    make_agent_loop,
    tool_pins: FakeToolPins,
) -> None:
    """写 Tool 缺执行 Profile:不执行外部调用,Run 以稳定失败码结束。"""
    write_handler = FakeWriteHandler()
    pins = FakeToolPins(
        pins={
            WRITE_TOOL_KEY: replace(
                tool_pins.pins[WRITE_TOOL_KEY],
                handler=write_handler,
                output_schema=TEST_OUTPUT_SCHEMA,
                execution_profile=None,
            )
        }
    )

    result = await make_agent_loop(
        backend=SequencedBackend([_write_round()]),
        pipeline=ScriptedPipeline([allow()]),
        tool_pins_override=pins,
    ).advance(seeded_run.id)

    assert result.run_status is RunStatus.FAILED
    assert result.failure_code == "WRITE_EXECUTION_PROFILE_MISSING"
    assert write_handler.calls == []


async def test_denied_tool_call_does_not_interrupt_loop(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
) -> None:
    read_handler = FakeReadHandler()
    loop = make_agent_loop(
        backend=SequencedBackend(
            [
                [
                    ToolCallCompleted(call_id="c0", name=READ_TOOL_KEY, arguments={}),
                    ToolCallCompleted(call_id="c1", name=READ_TOOL_KEY, arguments={}),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        ),
        pipeline=ScriptedPipeline(
            [deny(POLICY_DENIED, safe_message="not allowed"), allow()]
        ),
        read_handler=read_handler,
    )

    result = await loop.advance(seeded_run.id)

    assert result.processed_ordinals == [0, 1]
    assert result.run_status is RunStatus.COMPLETED
    assert len(read_handler.calls) == 1

    calls = await _load_tool_calls(db_session, seeded_run)
    assert calls[0].status is ToolCallStatus.DENIED
    assert calls[1].status is ToolCallStatus.SUCCEEDED

    events = await EventRepository(db_session).list_for_run(
        workspace_id=seeded_run.workspace_id, run_id=seeded_run.id
    )
    denied = [event for event in events if event.event_type == "TOOL_CALL_DENIED"]
    assert len(denied) == 1

    # 拒绝结果作为 ToolResult 回填给模型。
    tool_messages = (
        await db_session.scalars(
            select(Message).where(
                Message.run_id == seeded_run.id, Message.role == MessageRole.TOOL
            )
        )
    ).all()
    assert len(tool_messages) == 2


# ---------------------------------------------------------------------------
# 挂起恢复:先处理挂起 ToolCall,再处理剩余 ordinal
# ---------------------------------------------------------------------------


async def test_recovery_processes_pending_tool_call_before_remaining_ordinals(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    run = seeded_run
    # -- advance 1:ordinal 0 进入 WAITING_INPUT,后续停止。
    first_loop = make_agent_loop(
        backend=SequencedBackend(
            [
                [
                    ToolCallCompleted(
                        call_id="c0", name=READ_TOOL_KEY, arguments={"listing_id": "L1"}
                    ),
                    ToolCallCompleted(
                        call_id="c1", name=READ_TOOL_KEY, arguments={"listing_id": "L2"}
                    ),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        ),
        pipeline=ScriptedPipeline([_require_input_decision()]),
    )
    first = await first_loop.advance(run.id)
    assert first.run_status is RunStatus.WAITING_INPUT

    # 回答 Input Request(真实服务合并获准字段),Run 回 QUEUED 并创建恢复 Job。
    input_service = InputService(
        db_session, workspace_id=run.workspace_id, actor_id=seeded_workspace.user.id
    )
    input_request = (
        await db_session.scalars(
            select(InputRequest).where(
                InputRequest.workspace_id == run.workspace_id,
                InputRequest.run_id == run.id,
            )
        )
    ).one()
    answer = await input_service.answer(
        input_request.id, {"store_id": "S1"}, now=fake_clock_now()
    )
    assert answer.merged_arguments["store_id"] == "S1"

    await run_service.transition_run(
        run_id=run.id,
        target_status=RunStatus.QUEUED,
        event_kind="RUN_RESUMED",
        event_payload={"reason": "INPUT_ANSWERED"},
        expected_status=RunStatus.WAITING_INPUT,
    )
    # -- advance 2:先重走挂起调用(ordinal 0),再处理剩余 ordinal 1,然后新模型轮次。
    read_handler = FakeReadHandler()
    second_pipeline = ScriptedPipeline([allow(), deny(POLICY_DENIED), allow()])
    second_loop = make_agent_loop(
        backend=SequencedBackend(
            [
                [
                    ToolCallCompleted(
                        call_id="c2", name=READ_TOOL_KEY, arguments={"listing_id": "L3"}
                    ),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        ),
        pipeline=second_pipeline,
        read_handler=read_handler,
    )
    second = await second_loop.advance(run.id)

    assert second.run_status is RunStatus.COMPLETED
    assert second.persisted_ordinals == [2]
    assert second.processed_ordinals == [0, 1, 2]

    calls = await _load_tool_calls(db_session, run)
    assert [call.ordinal for call in calls] == [0, 1, 2]
    assert [call.status for call in calls] == [
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.DENIED,
        ToolCallStatus.SUCCEEDED,
    ]
    # 顺序:先挂起调用、再剩余 ordinal、最后新调用。
    assert [request.tool_call_id for request in second_pipeline.evaluated] == [
        calls[0].id,
        calls[1].id,
        calls[2].id,
    ]
    # 回答字段经完整 PreToolUse 重走后进入执行。
    assert read_handler.calls[0]["normalized_arguments"]["store_id"] == "S1"


def fake_clock_now() -> datetime:
    """与 FakeClock 默认时刻一致的"当前时间"(供真实交互服务判定过期)。"""
    return datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 写操作:UNKNOWN 停止 + Reconciliation 恢复;REQUIRE_CONFIRMATION 挂起与恢复
# ---------------------------------------------------------------------------


def _write_round() -> list[BackendEvent]:
    return [
        ToolCallCompleted(
            call_id="c0", name=WRITE_TOOL_KEY, arguments={"listing_id": "L1"}
        ),
        BackendCompleted(finish_reason="tool_calls"),
    ]


async def test_unknown_write_stops_loop_and_exposes_reconcile_job(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
) -> None:
    run = seeded_run
    loop = make_agent_loop(
        backend=SequencedBackend([_write_round()]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=FakeWriteHandler(fail=True),
    )
    result = await loop.advance(run.id)

    assert result.processed_ordinals == [0]
    assert result.run_status is RunStatus.WAITING_RECONCILIATION
    assert result.next_step is RunNextStep.RECONCILE

    persisted = await _load_run(db_session, run)
    assert persisted.status is PersistenceRunStatus.WAITING_RECONCILIATION

    calls = await _load_tool_calls(db_session, run)
    assert calls[0].status is ToolCallStatus.UNKNOWN
    assert calls[0].operation_id is not None


async def test_reconciliation_applied_recovery_completes_run(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    run = seeded_run
    first = await make_agent_loop(
        backend=SequencedBackend([_write_round()]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=FakeWriteHandler(fail=True),
    ).advance(run.id)
    assert first.run_status is RunStatus.WAITING_RECONCILIATION

    calls = await _load_tool_calls(db_session, run)
    operation = (
        await db_session.scalars(
            select(Operation).where(
                Operation.workspace_id == run.workspace_id,
                Operation.operation_id == calls[0].operation_id,
            )
        )
    ).one()

    # 首版执行器直接回查(无 Job/租约):APPLIED 后 Run 回 QUEUED。
    reconciler = ReconciliationService(
        db_session,
        workspace_id=run.workspace_id,
        actor_id=seeded_workspace.user.id,
        run_id=run.id,
        operation_id=operation.operation_id,
        expected_state_version=(await _load_run(db_session, run)).state_version,
        reconcile_budget=TEST_LIMITS.reconcile_budget,
        write_retry_budget=TEST_LIMITS.write_retry_budget,
    )
    outcome = await reconciler.apply_result(ReconcileResult.APPLIED)
    assert outcome.run_status is RunStatus.QUEUED
    assert outcome.next_step is RunNextStep.ADVANCE

    # 回查 APPLIED 后 Run 已 QUEUED,Loop 直接推进完成。
    second = await make_agent_loop(
        backend=SequencedBackend([]),
        pipeline=ScriptedPipeline([]),
        write_handler=FakeWriteHandler(),
    ).advance(run.id)

    assert second.run_status is RunStatus.COMPLETED
    assert second.processed_ordinals == []


async def test_require_confirmation_assigns_operation_id_and_resumes_after_accept(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    run = seeded_run
    spec = ConfirmationSpec(
        tool_name=WRITE_TOOL_KEY,
        normalized_arguments={"listing_id": "L1"},
        resource_versions={"listing": "v1"},
    )
    write_handler = FakeWriteHandler()
    first_loop = make_agent_loop(
        backend=SequencedBackend([_write_round()]),
        pipeline=ScriptedPipeline([require_confirmation(confirmation_spec=spec)]),
        write_handler=write_handler,
    )
    first = await first_loop.advance(run.id)

    assert first.processed_ordinals == [0]
    assert first.run_status is RunStatus.WAITING_CONFIRMATION

    calls = await _load_tool_calls(db_session, run)
    assert calls[0].status is ToolCallStatus.WAITING_CONFIRMATION
    # 写 Tool 在首次进入 Confirmation 前分配稳定 operation_id。
    operation_id = calls[0].operation_id
    assert operation_id is not None

    confirmation = (
        await db_session.scalars(
            select(ConfirmationRequest).where(
                ConfirmationRequest.workspace_id == run.workspace_id,
                ConfirmationRequest.run_id == run.id,
            )
        )
    ).one()
    assert confirmation.status.value == "PENDING"
    assert confirmation.operation_id == operation_id
    assert confirmation.fingerprint

    # 接受确认 → Run QUEUED + 恢复 Job;重入时重走 PreToolUse(ALLOW)并执行。
    confirmer = ConfirmationService(
        db_session, workspace_id=run.workspace_id, actor_id=seeded_workspace.user.id
    )
    accepted = await confirmer.accept(confirmation.id, now=fake_clock_now())
    assert accepted.status.value == "ACCEPTED"
    await run_service.transition_run(
        run_id=run.id,
        target_status=RunStatus.QUEUED,
        event_kind="RUN_RESUMED",
        event_payload={"reason": "CONFIRMATION_ACCEPTED"},
        expected_status=RunStatus.WAITING_CONFIRMATION,
    )

    second = await make_agent_loop(
        backend=SequencedBackend([]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=write_handler,
    ).advance(run.id)

    assert second.run_status is RunStatus.COMPLETED
    assert len(write_handler.calls) == 1
    # 复用确认前分配的稳定 operation_id(不重新生成)。
    assert write_handler.calls[0]["operation_id"] == operation_id


async def _drive_write_to_confirmation_and_accept(
    *,
    make_agent_loop,
    db_session: AsyncSession,
    run: Run,
    run_service: RunService,
    actor_id: uuid.UUID,
    write_handler: FakeWriteHandler,
) -> None:
    """驱动写 Tool 到人工确认并接受(Run 已回到 QUEUED)。"""
    spec = ConfirmationSpec(
        tool_name=WRITE_TOOL_KEY,
        normalized_arguments={"listing_id": "L1"},
        resource_versions={"listing": "v1"},
    )
    first = await make_agent_loop(
        backend=SequencedBackend([_write_round()]),
        pipeline=ScriptedPipeline([require_confirmation(confirmation_spec=spec)]),
        write_handler=write_handler,
    ).advance(run.id)
    assert first.run_status is RunStatus.WAITING_CONFIRMATION

    confirmation = (
        await db_session.scalars(
            select(ConfirmationRequest).where(
                ConfirmationRequest.workspace_id == run.workspace_id,
                ConfirmationRequest.run_id == run.id,
            )
        )
    ).one()
    accepted = await ConfirmationService(
        db_session, workspace_id=run.workspace_id, actor_id=actor_id
    ).accept(confirmation.id, now=fake_clock_now())
    assert accepted.status.value == "ACCEPTED"
    await run_service.transition_run(
        run_id=run.id,
        target_status=RunStatus.QUEUED,
        event_kind="RUN_RESUMED",
        event_payload={"reason": "CONFIRMATION_ACCEPTED"},
        expected_status=RunStatus.WAITING_CONFIRMATION,
    )


async def test_deterministic_rejection_is_executed_once_without_requeue(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    """确定性拒绝只执行一次:ToolCall 落 FAILED,不再重新入队。

    NOT_APPLIED 不等于"值得重试":把两者画等号会让同一个 ToolCall 反复经
    PreToolUse → prepare → 拒绝,永远走不到终态。
    """
    run = seeded_run
    write_handler = FakeWriteHandler(
        rejection=PreparedWriteRejection(
            status="FAILED", error_code="RESOURCE_CHANGED", context={"listing_id": "L1"}
        )
    )
    await _drive_write_to_confirmation_and_accept(
        make_agent_loop=make_agent_loop,
        db_session=db_session,
        run=run,
        run_service=run_service,
        actor_id=seeded_workspace.user.id,
        write_handler=write_handler,
    )
    second = await make_agent_loop(
        backend=SequencedBackend([]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=write_handler,
    ).advance(run.id)

    # 模型拿到失败 Tool Result 后收尾:Run 直接完成,不重新排队。
    assert second.run_status is RunStatus.COMPLETED
    assert second.next_step is None
    calls = await _load_tool_calls(db_session, run)
    assert calls[0].status is ToolCallStatus.FAILED
    assert calls[0].failure_code == "RESOURCE_CHANGED"
    assert write_handler.prepare_calls == 1
    assert write_handler.calls == []


async def test_explicitly_retryable_write_failure_requeues_for_revalidation(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    """回执显式声明可重试时才重排队:ToolCall 回 VALIDATING 并重新入队。"""
    run = seeded_run
    write_handler = FakeWriteHandler(
        execution_status="FAILED", execution_retryable=True
    )
    await _drive_write_to_confirmation_and_accept(
        make_agent_loop=make_agent_loop,
        db_session=db_session,
        run=run,
        run_service=run_service,
        actor_id=seeded_workspace.user.id,
        write_handler=write_handler,
    )
    second = await make_agent_loop(
        backend=SequencedBackend([]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=write_handler,
    ).advance(run.id)

    assert second.run_status is RunStatus.RUNNING
    assert second.next_step is RunNextStep.ADVANCE
    calls = await _load_tool_calls(db_session, run)
    assert calls[0].status is ToolCallStatus.VALIDATING
    assert len(write_handler.calls) == 1


async def test_deterministic_adapter_failure_is_not_requeued(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    """适配器确定失败且未声明可重试:ToolCall FAILED,Run 继续由模型收尾。"""
    run = seeded_run
    write_handler = FakeWriteHandler(execution_status="FAILED")
    await _drive_write_to_confirmation_and_accept(
        make_agent_loop=make_agent_loop,
        db_session=db_session,
        run=run,
        run_service=run_service,
        actor_id=seeded_workspace.user.id,
        write_handler=write_handler,
    )
    second = await make_agent_loop(
        backend=SequencedBackend([]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=write_handler,
    ).advance(run.id)

    assert second.run_status is RunStatus.COMPLETED
    assert second.next_step is None
    calls = await _load_tool_calls(db_session, run)
    assert calls[0].status is ToolCallStatus.FAILED
    # 外部调用只发生过一次:不重排队就不会有第二次 Adapter 调用。
    assert len(write_handler.calls) == 1
    assert write_handler.prepare_calls == 1


async def test_version_conflict_failure_is_terminal_and_returned_to_model(
    make_agent_loop,
    seeded_run: Run,
    db_session: AsyncSession,
    seeded_workspace,
    run_service: RunService,
) -> None:
    """``VERSION_CONFLICT`` 不重排同一个 ToolCall:失败返回模型。

    同一 ``operation_id`` 下 ``expected_version`` 不允许改写,平台也已按该
    ``operation_id`` 记录失败回执——重复提交只会回放旧结论。因此必须把失败
    返回模型(重新读取资源后创建新的 ToolCall/Operation),而不是把 ToolCall
    重排队回 VALIDATING。
    """
    run = seeded_run
    write_handler = FakeWriteHandler(
        execution_status="FAILED", execution_error_code="VERSION_CONFLICT"
    )
    await _drive_write_to_confirmation_and_accept(
        make_agent_loop=make_agent_loop,
        db_session=db_session,
        run=run,
        run_service=run_service,
        actor_id=seeded_workspace.user.id,
        write_handler=write_handler,
    )
    second = await make_agent_loop(
        backend=SequencedBackend([]),
        pipeline=ScriptedPipeline([allow()]),
        write_handler=write_handler,
    ).advance(run.id)

    assert second.run_status is RunStatus.COMPLETED
    assert second.next_step is None
    calls = await _load_tool_calls(db_session, run)
    assert calls[0].status is ToolCallStatus.FAILED
    assert calls[0].failure_code == "VERSION_CONFLICT"
    assert len(write_handler.calls) == 1
    assert write_handler.prepare_calls == 1


# ---------------------------------------------------------------------------
# 预算触发与 Backend 错误
# ---------------------------------------------------------------------------


async def test_tool_call_limit_exceeded(
    db_session: AsyncSession, make_agent_loop, seeded_run: Run
) -> None:
    loop = make_agent_loop(
        backend=SequencedBackend(
            [
                [
                    ToolCallCompleted(call_id="c0", name=READ_TOOL_KEY, arguments={}),
                    ToolCallCompleted(call_id="c1", name=READ_TOOL_KEY, arguments={}),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        ),
        pipeline=ScriptedPipeline([allow(), allow()]),
        read_handler=FakeReadHandler(),
        limits=replace(TEST_LIMITS, max_tool_calls=1),
    )
    result = await loop.advance(seeded_run.id)

    assert result.persisted_ordinals == [0, 1]
    assert result.processed_ordinals == [0]
    assert result.failure_code == "TOOL_CALL_LIMIT_EXCEEDED"
    assert result.run_status is RunStatus.FAILED


async def test_tool_timeout_fails_run(
    make_agent_loop, seeded_run: Run, db_session: AsyncSession, fake_clock: FakeClock
) -> None:
    loop = make_agent_loop(
        backend=SequencedBackend(
            [
                [
                    ToolCallCompleted(call_id="c0", name=READ_TOOL_KEY, arguments={}),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        ),
        pipeline=ScriptedPipeline([allow()]),
        read_handler=FakeReadHandler(
            fake_clock, timeout_seconds=TEST_LIMITS.tool_timeout_seconds + 1
        ),
        clock=fake_clock,
    )
    result = await loop.advance(seeded_run.id)

    assert result.failure_code == "TOOL_TIMEOUT"
    assert result.run_status is RunStatus.FAILED
    calls = await _load_tool_calls(db_session, seeded_run)
    assert calls[0].status is ToolCallStatus.FAILED


async def test_run_deadline_exceeded(
    make_agent_loop, seeded_run: Run, db_session: AsyncSession, fake_clock: FakeClock
) -> None:
    seeded_run.deadline_at = fake_clock.now() - timedelta(seconds=1)
    await db_session.flush()

    result = await make_agent_loop(
        backend=SequencedBackend([[BackendCompleted(finish_reason="stop")]]),
        pipeline=ScriptedPipeline([]),
    ).advance(seeded_run.id)

    assert result.failure_code == "RUN_DEADLINE_EXCEEDED"
    assert result.run_status is RunStatus.FAILED


async def test_backend_retryable_failure_creates_new_attempt(
    make_agent_loop, seeded_run: Run, db_session: AsyncSession
) -> None:
    loop = make_agent_loop(
        backend=SequencedBackend(
            [
                [BackendFailed(error_code="PROVIDER_RATE_LIMIT", retryable=True)],
                [TextDelta(text_delta="recovered"), BackendCompleted(finish_reason="stop")],
            ]
        ),
        pipeline=ScriptedPipeline([]),
    )
    result = await loop.advance(seeded_run.id)

    assert result.run_status is RunStatus.COMPLETED
    attempts = (
        await db_session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == seeded_run.id)
        )
    ).all()
    assert len(attempts) == 1


async def test_backend_non_retryable_failure_fails_run(
    make_agent_loop, seeded_run: Run, db_session: AsyncSession
) -> None:
    loop = make_agent_loop(
        backend=SequencedBackend(
            [[BackendFailed(error_code="PROVIDER_AUTH", retryable=False)]]
        ),
        pipeline=ScriptedPipeline([]),
    )
    result = await loop.advance(seeded_run.id)

    assert result.failure_code == "PROVIDER_AUTH"
    assert result.run_status is RunStatus.FAILED

    persisted = await _load_run(db_session, seeded_run)
    assert persisted.failure_code == "PROVIDER_AUTH"
    messages = (
        await db_session.scalars(select(Message).where(Message.run_id == seeded_run.id))
    ).all()
    assert [message.status for message in messages] == [MessageStatus.ABORTED]
    # 不可重试错误不创建新 Attempt。
    attempts = (
        await db_session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == seeded_run.id)
        )
    ).all()
    assert attempts == []


# ---------------------------------------------------------------------------
# Runtime 组合根接线
# ---------------------------------------------------------------------------


class _RuntimeDriver:
    """把 Runtime 适配成 RunDriver;回查在首版测试里不启用。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def advance(self, claim: RunClaim, *, answered=None) -> AdvanceOutcome:
        del answered
        return await self._runtime.advance(claim)

    async def reconcile(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        del run_id, workspace_id
        return False


async def _seed_committed_runtime_scenario(engine: AsyncEngine) -> dict[str, Any]:
    """提交最小种子链 + 两个 QUEUED Run + Tool 行。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
            user = User(id=uuid.uuid4(), login=f"user-{uuid.uuid4()}", password_hash="test-hash")
            membership = WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.OPERATOR,
            )
            agent = Agent(id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4()}", name="test-agent")
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=1,
                definition_json={},
                definition_hash="h",
            )
            model_connection = ModelConnection(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                stable_key=f"model-{uuid.uuid4()}",
                name="test-model",
            )
            model_config_version = ModelConfigVersion(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                model_connection_id=model_connection.id,
                version=1,
                backend_type=ModelBackendType.OPENAI,
                base_url="https://api.example.com/v1",
                model="gpt-test",
                non_secret_options={},
                config_hash="h",
            )
            # 按外键依赖分批 flush,保证被引用行先落库。
            session.add_all([workspace, user, agent])
            await session.flush()
            session.add_all([membership, agent_version, model_connection])
            await session.flush()
            session.add(model_config_version)
            await session.flush()

            session_one = await SessionRepository(session).create(
                workspace_id=workspace.id,
                agent_id=agent.id,
                agent_version_id=agent_version.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
            )
            session_two = await SessionRepository(session).create(
                workspace_id=workspace.id,
                agent_id=agent.id,
                agent_version_id=agent_version.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
            )
            run_to_advance = await RunRepository(session).create(
                workspace_id=workspace.id,
                session_id=session_one.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
                agent_version_id=agent_version.id,
                model_config_version_id=model_config_version.id,
            )
            run_to_recover = await RunRepository(session).create(
                workspace_id=workspace.id,
                session_id=session_two.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
                agent_version_id=agent_version.id,
                model_config_version_id=model_config_version.id,
            )
            read_def, read_ver = _tool_rows(READ_TOOL_KEY, ToolEffect.READ)
            session.add_all([read_def, read_ver])
            await session.flush()
            return {
                "workspace_id": workspace.id,
                "user_id": user.id,
                "agent_id": agent.id,
                "agent_version_id": agent_version.id,
                "model_connection_id": model_connection.id,
                "model_config_version_id": model_config_version.id,
                "run_to_advance_id": run_to_advance.id,
                "run_to_recover_id": run_to_recover.id,
                "tool_definition_id": read_def.id,
                "tool_version_id": read_ver.id,
            }
    finally:
        await session.close()


async def _cleanup_runtime_scenario(engine: AsyncEngine, ids: Mapping[str, Any]) -> None:
    """按外键依赖倒序删除已提交种子行,保持测试库干净。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            statements: list[tuple[str, dict[str, Any]]] = [
                ("DELETE FROM agent_events WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM operation_attempts WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM operations WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM tool_calls WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM run_attempts WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM input_requests WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM confirmation_requests WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM messages WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM run_tool_versions WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM run_skill_versions WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM run_source_versions WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM run_policy_snapshots WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM runs WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM sessions WHERE workspace_id = :ws", {"ws": ids["workspace_id"]}),
                ("DELETE FROM tool_versions WHERE tool_id = :tool", {"tool": ids["tool_definition_id"]}),
                ("DELETE FROM tool_definitions WHERE id = :tool", {"tool": ids["tool_definition_id"]}),
                (
                    "DELETE FROM model_config_versions WHERE id = :id",
                    {"id": ids["model_config_version_id"]},
                ),
                (
                    "DELETE FROM model_connections WHERE id = :id",
                    {"id": ids["model_connection_id"]},
                ),
                ("DELETE FROM agent_versions WHERE id = :id", {"id": ids["agent_version_id"]}),
                ("DELETE FROM agents WHERE id = :id", {"id": ids["agent_id"]}),
                (
                    "DELETE FROM workspace_memberships WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                ("DELETE FROM users WHERE id = :id", {"id": ids["user_id"]}),
                ("DELETE FROM workspaces WHERE id = :id", {"id": ids["workspace_id"]}),
            ]
            for text, params in statements:
                await session.execute(sa.text(text), params)
    finally:
        await session.close()


async def test_executor_advances_run_and_recovers_interrupted_run(
    engine: AsyncEngine,
) -> None:
    """执行器推进 QUEUED Run;RUNNING 残留由 recover 退回 QUEUED 后继续。"""
    ids = await _seed_committed_runtime_scenario(engine)
    try:
        pins = FakeToolPins(
            pins={
                READ_TOOL_KEY: ToolPin(
                    stable_key=READ_TOOL_KEY,
                    version=1,
                    version_id=ids["tool_version_id"],
                    effect=ToolEffect.READ,
                )
            }
        )
        runtime = Runtime(
            session_factory=lambda: AsyncSession(engine, expire_on_commit=False),
            backend=SequencedBackend(
                [
                    [BackendCompleted(finish_reason="stop")],
                    [BackendCompleted(finish_reason="stop")],
                ]
            ),
            assembler=make_assembler(),
            limits=TEST_LIMITS,
            tool_pins=pins,
            pipeline_factory=lambda: ScriptedPipeline([]),
        )
        executor = RunExecutor(
            session_factory=lambda: AsyncSession(engine, expire_on_commit=False),
            driver=_RuntimeDriver(runtime),
            instance_id="test-executor",
        )

        # QUEUED → RUNNING(CAS)→ 推进到 COMPLETED。
        assert await executor.advance_now(ids["run_to_advance_id"]) is not None
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            run = await session.get(Run, ids["run_to_advance_id"])
            assert run is not None and run.status is PersistenceRunStatus.COMPLETED
        finally:
            await session.close()

        # 模拟上次进程中断:Run 停在 RUNNING,恢复扫描应退回 QUEUED 并推进。
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            async with session.begin():
                await session.execute(
                    sa.update(Run)
                    .where(Run.id == ids["run_to_recover_id"])
                    .values(status="RUNNING", state_version=Run.state_version + 1)
                )
        finally:
            await session.close()
        report = await executor.recover()
        assert ids["run_to_recover_id"] in report.advanced
        session = AsyncSession(engine, expire_on_commit=False)
        try:
            run = await session.get(Run, ids["run_to_recover_id"])
            assert run is not None and run.status is PersistenceRunStatus.COMPLETED
        finally:
            await session.close()

        # 终态 Run 不在恢复范围内。
        assert await executor.advance_now(ids["run_to_advance_id"]) is None
    finally:
        await _cleanup_runtime_scenario(engine, ids)


async def test_runtime_does_not_hold_database_transaction_while_backend_waits(
    engine: AsyncEngine,
) -> None:
    ids = await _seed_committed_runtime_scenario(engine)
    backend_started = asyncio.Event()
    release_backend = asyncio.Event()
    host_sessions: list[AsyncSession] = []

    class BlockingBackend(SequencedBackend):
        async def stream(
            self, request: BackendRequest, cancellation: CancellationToken
        ) -> AsyncIterator[BackendEvent]:
            del request, cancellation
            backend_started.set()
            yield_transaction_open.append(host_sessions[-1].in_transaction())
            await release_backend.wait()
            yield BackendCompleted(finish_reason="stop")

    yield_transaction_open: list[bool] = []

    def session_factory() -> AsyncSession:
        session = AsyncSession(engine, expire_on_commit=False)
        host_sessions.append(session)
        return session

    runtime = Runtime(
        session_factory=session_factory,
        backend=BlockingBackend([]),
        assembler=make_assembler(),
        limits=TEST_LIMITS,
        tool_pins=FakeToolPins(pins={}),
        pipeline_factory=lambda: ScriptedPipeline([]),
    )
    executor = RunExecutor(
        session_factory=session_factory,
        driver=_RuntimeDriver(runtime),
        instance_id="test-executor",
    )
    worker_task = asyncio.create_task(executor.advance_now(ids["run_to_advance_id"]))
    try:
        await asyncio.wait_for(backend_started.wait(), timeout=2)
        assert yield_transaction_open == [False]
        release_backend.set()
        assert await worker_task is not None
    finally:
        release_backend.set()
        if not worker_task.done():
            await worker_task
        await _cleanup_runtime_scenario(engine, ids)
