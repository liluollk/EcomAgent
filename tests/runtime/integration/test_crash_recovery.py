"""崩溃恢复集成测试(计划 02 Task 10;架构文档 §8、§13、§14;ADR-003)。

以 ``crash_harness`` 夹具封装故障注入:seed Run → 以受控故障点运行
Runtime/AgentLoop → 恢复 → 断言。外部世界(模型流、写 Tool handler、
回查适配器)用确定性 fake 注入;状态机、执行器领取、回查与事件全部走真实
Runtime 服务与真库(沿用 tests/runtime 现有模式:种子行提交 + 独立
Session 工厂 + 结束清理,因 Runtime 使用独立 Session)。

覆盖场景(首版单执行器模型):
1. 执行前进程崩溃(Run 停在 RUNNING;重启扫描收尾遗留 Attempt 并继续推进);
2. Operation 已改 EXECUTING 后进程崩溃(EXECUTING 同样是不确定写,先回查);
3. 外部写入生效但响应丢失(回查 APPLIED,不二次写);
4. 回查 UNKNOWN → APPLIED(预算内继续回查,消除不确定性后恢复推进);
5. 同一 Run 的第二个推进者被 ``state_version`` CAS 拒绝;
6. ToolResult Schema 失败不清除已发生写入(APPLIED 保持、权威事实不改写);
7. Context 压缩不丢 Protected Context(安全规则、当前用户消息、待确认
   fingerprint 在超预算组装结果中)。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendEvent,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.executor import AdvanceOutcome, RunClaim, RunExecutor
from runtime.execution.operations import (
    BegunOperation,
    OperationOutcome,
    OperationService,
)
from runtime.execution.pretool.decisions import allow
from runtime.execution.pretool.pipeline import ToolCallRequest
from runtime.execution.reconciliation import (
    ReconcileOutcome,
    ReconcileResult,
    ReconciliationService,
)
from runtime.execution.tool_executor import PreparedToolWrite
from runtime.execution.tool_result_processor import (
    ToolExecutionReceipt,
    ToolResultProcessor,
)
from runtime.interfaces.state import AssistantMessageStatus, MessageType, RunStatus
from runtime.orchestration.agent_loop import ToolPin
from runtime.orchestration.context import (
    AssembledContext,
    ContextAssembler,
    ConversationMessage,
    ConversationView,
    MemoryItem,
    PendingConfirmationView,
    ToolResultProjection,
)
from runtime.orchestration.limits import RunLimits
from runtime.orchestration.runtime import Runtime
from runtime.orchestration.summarization import ConversationSummarizer
from runtime.persistence.base import (
    ConfigScope,
    MembershipRole,
    MessageRole,
    MessageStatus,
    ModelBackendType,
    OperationStatus,
    RunAttemptStatus,
    RunNextStep,
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
from runtime.persistence.models.interaction import Message, Session
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


AFTER_EXTERNAL_WRITE_BEFORE_RESULT = "AFTER_EXTERNAL_WRITE_BEFORE_RESULT"
"""受控故障点:外部写入已生效、结果尚未提交时 Worker 崩溃。"""


def _stable_key(prefix: str) -> str:
    """GLOBAL ToolDefinition 的 stable_key 有唯一约束;每个 harness 实例唯一。"""
    return f"{prefix}-{uuid.uuid4()}"

TEST_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
}

_CRASH_ADAPTER_KEY = "crash-adapter"
"""崩溃恢复测试写 Tool 的执行 Profile Adapter Key。"""

TEST_LIMITS = RunLimits(
    max_model_rounds=4,
    max_tool_calls=5,
    tool_timeout_seconds=60,
    run_deadline_seconds=3600,
    backend_retries=1,
    reconcile_budget=3,
)

SUMMARY_PAYLOAD_JSON = '{"schema_version": 1, "summary": "digest", "key_facts": []}'


# ---------------------------------------------------------------------------
# 确定性替身(外部世界注入)
# ---------------------------------------------------------------------------


class SequencedBackend:
    """按轮次回放事件脚本:每次 stream 消耗一个脚本,耗尽后返回空完成。"""

    def __init__(self, rounds: Sequence[Sequence[BackendEvent]]) -> None:
        self.rounds: list[list[BackendEvent]] = [list(round) for round in rounds]

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        events = (
            self.rounds.pop(0) if self.rounds else [BackendCompleted(finish_reason="stop")]
        )
        for event in events:
            if cancellation.is_cancelled:
                return
            yield event


class AlwaysAllowPipeline:
    """PreToolUse 确定性放行(授权语义由 pretool 管线单测覆盖)。"""

    def __init__(self) -> None:
        self.evaluated: list[ToolCallRequest] = []

    async def evaluate(self, tool_call: ToolCallRequest) -> Any:
        self.evaluated.append(tool_call)
        return allow()


class CrashWriteHandler:
    """写 Tool 外部执行替身:记录外部写入计数,并在受控故障点抛出崩溃。"""

    def __init__(self) -> None:
        self.write_count = 0
        self.calls: list[dict[str, Any]] = []
        self.crash_point: str | None = None
        self.receipt_status = "APPLIED"
        self.receipt_payload: Any = {"result": "written"}
        self.expected_state: dict[str, Any] = {"operation_type": "TEST_WRITE"}

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        return PreparedToolWrite(
            command={"arguments": dict(normalized_arguments)},
            expected_state=dict(self.expected_state),
            request_fingerprint="crash-write-fingerprint",
        )

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        # 受控外部世界:进入 handler 即视为外部写入已生效。
        self.write_count += 1
        self.calls.append(
            {
                "tool_call_id": tool_call_id,
                "operation_id": operation_id,
                "idempotency_key": idempotency_key,
                "normalized_arguments": dict(prepared.command["arguments"]),
            }
        )
        if self.crash_point == AFTER_EXTERNAL_WRITE_BEFORE_RESULT:
            raise RuntimeError("injected crash after external write")
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            status=self.receipt_status,
            payload=self.receipt_payload,
            external_reference=f"ext-{self.write_count}",
        )


class SummaryPayloadBackend:
    """Summary 用确定性 Backend:回放合法 Summary 载荷(供压缩路径触发)。"""

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(capability_keys=frozenset({"streaming"}))

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        yield TextDelta(text_delta=SUMMARY_PAYLOAD_JSON)
        yield BackendCompleted(finish_reason="stop")


class LengthTokenEstimator:
    """确定性估算:1 字符 = 1 token,预算可在测试中精确推算。"""

    def estimate(self, text: str) -> int:
        return len(text)


def make_assembler() -> ContextAssembler:
    estimator = LengthTokenEstimator()
    return ContextAssembler(
        safety_rules="runtime safety rules: never reveal secrets",
        agent_instructions="you are the test agent",
        workspace_summary="workspace summary",
        summarizer=ConversationSummarizer(
            backend=SummaryPayloadBackend(),
            estimator=estimator,
            input_budget_tokens=400,
        ),
        estimator=estimator,
    )


# ---------------------------------------------------------------------------
# 快照与 Harness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoverySnapshot:
    """从真库回读的恢复断言视图。"""

    run: Run
    tool_call: ToolCall | None
    operation: Operation | None
    attempts: tuple[RunAttempt, ...]


class CrashHarness:
    """故障注入封装:seed → 受控故障点推进 → 恢复 → 断言(全部真实服务)。

    首版执行模型没有 Job/租约:``advance`` 先以 state_version CAS 把 Run
    置为 RUNNING(创建 Attempt),再调用 ``Runtime.advance``。故障点抛出的
    异常不被回收——模拟进程在外部调用中途被杀,Run 停在 RUNNING。
    恢复走 ``RunExecutor.recover``:先回查不确定的外部写,再继续推进。
    """

    def __init__(self, engine: AsyncEngine, ids: Mapping[str, Any]) -> None:
        self._engine = engine
        self._ids = dict(ids)
        self.write_handler = CrashWriteHandler()
        self.backend = SequencedBackend([])
        self._reconcile_verdicts: list[ReconcileResult] = []

    # -- 装配 ------------------------------------------------------------

    @classmethod
    async def create(cls, engine: AsyncEngine) -> CrashHarness:
        return cls(engine, await _seed_committed_scenario(engine))

    def set_backend_rounds(self, rounds: Sequence[Sequence[BackendEvent]]) -> None:
        self.backend = SequencedBackend(rounds)

    def set_receipt(self, *, status: str, payload: Any) -> None:
        self.write_handler.receipt_status = status
        self.write_handler.receipt_payload = payload

    def set_reconcile_verdicts(self, verdicts: Sequence[ReconcileResult]) -> None:
        self._reconcile_verdicts = list(verdicts)

    @property
    def external_write_count(self) -> int:
        return self.write_handler.write_count

    @property
    def run_id(self) -> uuid.UUID:
        return self._ids["run_id"]

    @property
    def write_tool_key(self) -> str:
        return self._ids["write_tool_key"]

    def write_round(self) -> list[BackendEvent]:
        """模型提出写 ToolCall 的一轮脚本(Tool 名与本 harness 的 Tool 版本对齐)。"""
        return [
            ToolCallCompleted(
                call_id="c0", name=self.write_tool_key, arguments={"listing_id": "L1"}
            ),
            BackendCompleted(finish_reason="tool_calls"),
        ]

    def _session(self) -> AsyncSession:
        return AsyncSession(self._engine, expire_on_commit=False)

    def _runtime(self) -> Runtime:
        return Runtime(
            session_factory=lambda: AsyncSession(self._engine, expire_on_commit=False),
            backend=self.backend,
            assembler=make_assembler(),
            limits=TEST_LIMITS,
            tool_pins=_write_tool_pins(
                self._ids["write_tool_key"],
                self._ids["tool_version_id"],
                self.write_handler,
            ),
            pipeline_factory=lambda: AlwaysAllowPipeline(),
        )

    def _executor(self) -> RunExecutor:
        return RunExecutor(
            session_factory=lambda: AsyncSession(self._engine, expire_on_commit=False),
            driver=self,
            instance_id="crash-executor",
        )

    # -- RunDriver -------------------------------------------------------

    async def advance(self, claim: RunClaim, *, answered: Any = None) -> AdvanceOutcome:
        del answered
        return await self._runtime().advance(claim)

    async def reconcile(self, run_id: uuid.UUID, workspace_id: uuid.UUID) -> bool:
        """回查该 Run 的 UNKNOWN Operation;返回是否可以继续推进。"""
        del workspace_id
        outcome = await self.apply_reconcile(self._next_verdict())
        return outcome.next_step is RunNextStep.ADVANCE

    def _next_verdict(self) -> ReconcileResult:
        if self._reconcile_verdicts:
            return self._reconcile_verdicts.pop(0)
        return ReconcileResult.APPLIED

    # -- 受控故障推进与恢复 ----------------------------------------------

    async def advance_until(self, crash_point: str | None = None) -> RecoverySnapshot:
        """推进一次;``crash_point`` 处抛异常并放弃收尾(模拟进程被杀)。"""
        self.write_handler.crash_point = crash_point
        claim = await self.claim()
        if claim is None:
            self.write_handler.crash_point = None
            return await self.snapshot()
        try:
            await self._runtime().advance(claim)
        except RuntimeError:
            if crash_point is None:
                raise
        finally:
            self.write_handler.crash_point = None
        return await self.snapshot()

    async def state_version(self) -> int:
        """读取 Run 当前 ``state_version``(模拟另一个推进者的读视图)。"""
        session = self._session()
        try:
            run = await _fresh_run(
                session, self._ids["workspace_id"], self._ids["run_id"]
            )
            return run.state_version
        finally:
            await session.close()

    async def try_cas(self, expected_version: int) -> bool:
        """以过期版本直接 CAS:零行更新即被并发保护拒绝。"""
        session = self._session()
        try:
            async with session.begin():
                return await RunRepository(session).compare_and_set_status(
                    workspace_id=self._ids["workspace_id"],
                    run_id=self._ids["run_id"],
                    expected_version=expected_version,
                    new_status=PersistenceRunStatus.RUNNING.value,
                    expected_status=PersistenceRunStatus.QUEUED.value,
                )
        finally:
            await session.close()

    async def claim(self) -> RunClaim | None:
        """以 state_version CAS 把 Run 推入 RUNNING 并创建 Attempt。

        返回 None 表示 Run 已不在 QUEUED(终态或已被推进)。
        """
        session = self._session()
        try:
            async with session.begin():
                run = await _fresh_run(
                    session, self._ids["workspace_id"], self._ids["run_id"]
                )
                if run.status is not PersistenceRunStatus.QUEUED:
                    return None
                moved = await RunRepository(session).compare_and_set_status(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    expected_version=run.state_version,
                    new_status=PersistenceRunStatus.RUNNING.value,
                    expected_status=PersistenceRunStatus.QUEUED.value,
                )
                if not moved:
                    return None
                attempt_no = (
                    await session.scalar(
                        sa.select(sa.func.max(RunAttempt.attempt_no)).where(
                            RunAttempt.run_id == run.id
                        )
                    )
                    or 0
                ) + 1
                attempt = RunAttempt(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    attempt_no=attempt_no,
                    executor_id="crash-executor",
                    status="RUNNING",
                    started_at=datetime.now(UTC),
                )
                session.add(attempt)
                await session.flush()
                return RunClaim(
                    run_id=run.id,
                    workspace_id=run.workspace_id,
                    actor_id=run.created_by_actor_id,
                    membership_id=run.created_by_membership_id,
                    attempt_id=attempt.id,
                    attempt_no=attempt_no,
                )
        finally:
            await session.close()

    async def recover(self) -> RecoverySnapshot:
        """进程重启恢复:先回查不确定的外部写,再继续推进。"""
        await self._executor().recover()
        return await self.snapshot()

    async def apply_reconcile(self, verdict: ReconcileResult) -> ReconcileOutcome:
        """以真实 ReconciliationService 提交一次回查结论(无租约)。"""
        operation_id = await self._load_operation_id()
        session = self._session()
        try:
            async with session.begin():
                run = await _fresh_run(
                    session, self._ids["workspace_id"], self._ids["run_id"]
                )
                service = ReconciliationService(
                    session,
                    workspace_id=self._ids["workspace_id"],
                    actor_id=self._ids["user_id"],
                    run_id=self._ids["run_id"],
                    operation_id=operation_id,
                    expected_state_version=run.state_version,
                    reconcile_budget=TEST_LIMITS.reconcile_budget,
                    write_retry_budget=TEST_LIMITS.write_retry_budget,
                )
                return await service.apply_result(verdict)
        finally:
            await session.close()

    # -- 低层边界 --------------------------------------------------------

    async def seed_pending_write_tool_call(self) -> ToolCall:
        """为"执行边界已提交"场景 seed VALIDATING ToolCall(执行 conftest 同模式)。"""
        session = self._session()
        try:
            async with session.begin():
                run = await _fresh_run(
                    session, self._ids["workspace_id"], self._ids["run_id"]
                )
                message = Message(
                    workspace_id=run.workspace_id,
                    session_id=run.session_id,
                    run_id=run.id,
                    role=MessageRole.ASSISTANT,
                    status=MessageStatus.COMPLETED,
                    content="call write tool",
                    sequence_no=1,
                )
                session.add(message)
                await session.flush()
                tool_call = ToolCall(
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    message_id=message.id,
                    tool_version_id=self._ids["tool_version_id"],
                    ordinal=1,
                    status="VALIDATING",
                    raw_arguments={"listing_id": "L1"},
                    normalized_arguments={"listing_id": "L1"},
                )
                session.add(tool_call)
                await session.flush()
                return tool_call
        finally:
            await session.close()

    async def begin_operation(self) -> BegunOperation:
        """执行边界短事务:OperationService.begin 提交 EXECUTING 后进程退出。"""
        session = self._session()
        try:
            async with session.begin():
                run = await _fresh_run(
                    session, self._ids["workspace_id"], self._ids["run_id"]
                )
                tool_call = await self._load_tool_call(session)
                service = OperationService(
                    session,
                    workspace_id=self._ids["workspace_id"],
                    actor_id=self._ids["user_id"],
                    run_id=self._ids["run_id"],
                    expected_state_version=run.state_version,
                )
                return await service.begin(
                    tool_call.id,
                    adapter_key=_CRASH_ADAPTER_KEY,
                    expected_state=dict(self.write_handler.expected_state),
                    supports_reconciliation=True,
                )
        finally:
            await session.close()

    # -- 断言辅助 --------------------------------------------------------

    async def snapshot(self) -> RecoverySnapshot:
        session = self._session()
        try:
            run = await _fresh_run(session, self._ids["workspace_id"], self._ids["run_id"])
            tool_call = await self._load_tool_call(session, required=False)
            operation: Operation | None = None
            if tool_call is not None:
                operation = (
                    await session.scalars(
                        sa.select(Operation)
                        .where(
                            Operation.workspace_id == self._ids["workspace_id"],
                            Operation.tool_call_id == tool_call.id,
                        )
                        .limit(1)
                    )
                ).one_or_none()
            attempts = (
                await session.scalars(
                    sa.select(RunAttempt)
                    .where(
                        RunAttempt.workspace_id == self._ids["workspace_id"],
                        RunAttempt.run_id == self._ids["run_id"],
                    )
                    .order_by(RunAttempt.attempt_no)
                )
            ).all()
            return RecoverySnapshot(
                run=run,
                tool_call=tool_call,
                operation=operation,
                attempts=tuple(attempts),
            )
        finally:
            await session.close()

    async def _load_tool_call(
        self, session: AsyncSession, *, required: bool = True
    ) -> ToolCall | None:
        stmt = (
            sa.select(ToolCall)
            .where(
                ToolCall.workspace_id == self._ids["workspace_id"],
                ToolCall.run_id == self._ids["run_id"],
            )
            .execution_options(populate_existing=True)
        )
        tool_call = (await session.scalars(stmt)).one_or_none()
        if required:
            assert tool_call is not None
        return tool_call

    async def _load_operation_id(self) -> uuid.UUID:
        session = self._session()
        try:
            tool_call = await self._load_tool_call(session)
            assert tool_call is not None
            stmt = sa.select(Operation.operation_id).where(
                Operation.workspace_id == self._ids["workspace_id"],
                Operation.tool_call_id == tool_call.id,
            )
            operation_id = (await session.scalars(stmt)).one_or_none()
            assert operation_id is not None
            return operation_id
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# 种子与清理(已提交种子行 + 独立 Session 工厂模式,见编排层测试)
# ---------------------------------------------------------------------------


class _WriteToolPins:
    """内存 ToolPin 解析器:仅解析集成测试 seed 的写 Tool 版本。"""

    def __init__(self, stable_key: str, tool_version_id: uuid.UUID, handler: Any) -> None:
        self._key = stable_key
        self._pin = ToolPin(
            stable_key=stable_key,
            version=1,
            version_id=tool_version_id,
            effect=ToolEffect.WRITE,
            handler=handler,
            output_schema=TEST_OUTPUT_SCHEMA,
            execution_profile=ToolExecutionProfile(
                adapter_key=_CRASH_ADAPTER_KEY,
                supports_reconciliation=True,
            ),
        )

    def by_stable_key(self, stable_key: str) -> ToolPin | None:
        return self._pin if stable_key == self._key else None

    def by_version_id(self, version_id: uuid.UUID) -> ToolPin | None:
        return self._pin if version_id == self._pin.version_id else None


def _write_tool_pins(
    stable_key: str, tool_version_id: uuid.UUID, handler: Any
) -> _WriteToolPins:
    return _WriteToolPins(stable_key, tool_version_id, handler)


async def _seed_committed_scenario(engine: AsyncEngine) -> dict[str, Any]:
    """提交最小种子链:身份、Session、Run 与写 Tool 行。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
            user = User(
                id=uuid.uuid4(), login=f"user-{uuid.uuid4()}", password_hash="test-hash"
            )
            membership = WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.OPERATOR,
            )
            agent = Agent(
                id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4()}", name="test-agent"
            )
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=1,
                definition_json={},
                definition_hash="test-hash",
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
                config_hash="test-hash",
            )
            # 模型间无 relationship() 声明,按外键依赖分批 flush。
            session.add_all([workspace, user, agent])
            await session.flush()
            session.add_all([membership, agent_version, model_connection])
            await session.flush()
            session.add(model_config_version)
            await session.flush()

            session_row: Session = await SessionRepository(session).create(
                workspace_id=workspace.id,
                agent_id=agent.id,
                agent_version_id=agent_version.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
            )
            run = await RunRepository(session).create(
                workspace_id=workspace.id,
                session_id=session_row.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
                agent_version_id=agent_version.id,
                model_config_version_id=model_config_version.id,
            )
            write_tool_key = _stable_key("write-tool")
            tool_definition = ToolDefinition(
                id=uuid.uuid4(),
                scope=ConfigScope.GLOBAL,
                workspace_id=None,
                stable_key=write_tool_key,
                name=write_tool_key,
            )
            tool_version = ToolVersion(
                id=uuid.uuid4(),
                workspace_id=None,
                tool_id=tool_definition.id,
                version=1,
                effect=ToolEffect.WRITE,
                input_schema={"type": "object"},
                output_schema=TEST_OUTPUT_SCHEMA,
                schema_hash="schema-hash-1",
            )
            session.add_all([tool_definition, tool_version])
            await session.flush()
            return {
                "workspace_id": workspace.id,
                "user_id": user.id,
                "agent_id": agent.id,
                "agent_version_id": agent_version.id,
                "model_connection_id": model_connection.id,
                "model_config_version_id": model_config_version.id,
                "run_id": run.id,
                "write_tool_key": write_tool_key,
                "tool_definition_id": tool_definition.id,
                "tool_version_id": tool_version.id,
            }
    finally:
        await session.close()


async def _fresh_run(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> Run:
    return (
        await session.scalars(
            sa.select(Run)
            .where(Run.workspace_id == workspace_id, Run.id == run_id)
            .execution_options(populate_existing=True)
        )
    ).one()


async def _cleanup_scenario(engine: AsyncEngine, ids: Mapping[str, Any]) -> None:
    """按外键依赖倒序删除已提交种子行,保持测试库干净。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            statements: list[tuple[str, dict[str, Any]]] = [
                # 写路径审计(TOOL_WRITE_* 等)引用 runs/tool_calls,最先删除。
                (
                    "DELETE FROM audit_events WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM agent_events WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM operation_attempts WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                # operations 与 tool_calls 互引用(复合外键);先解除
                # tool_calls.operation_id 引用再删除两侧,避免循环依赖。
                (
                    "UPDATE tool_calls SET operation_id = NULL WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM operations WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM tool_calls WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM run_attempts WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM input_requests WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM confirmation_requests WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM messages WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM run_tool_versions WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM run_skill_versions WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM run_source_versions WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM run_policy_snapshots WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM runs WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM sessions WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                (
                    "DELETE FROM tool_versions WHERE tool_id = :tool",
                    {"tool": ids["tool_definition_id"]},
                ),
                (
                    "DELETE FROM tool_definitions WHERE id = :tool",
                    {"tool": ids["tool_definition_id"]},
                ),
                (
                    "DELETE FROM model_config_versions WHERE id = :id",
                    {"id": ids["model_config_version_id"]},
                ),
                (
                    "DELETE FROM model_connections WHERE id = :id",
                    {"id": ids["model_connection_id"]},
                ),
                (
                    "DELETE FROM agent_versions WHERE id = :id",
                    {"id": ids["agent_version_id"]},
                ),
                ("DELETE FROM agents WHERE id = :id", {"id": ids["agent_id"]}),
                (
                    "DELETE FROM workspace_memberships WHERE workspace_id = :ws",
                    {"ws": ids["workspace_id"]},
                ),
                ("DELETE FROM users WHERE id = :id", {"id": ids["user_id"]}),
                (
                    "DELETE FROM workspaces WHERE id = :id",
                    {"id": ids["workspace_id"]},
                ),
            ]
            for text, params in statements:
                await session.execute(sa.text(text), params)
    finally:
        await session.close()


@pytest.fixture()
async def crash_harness(engine: AsyncEngine) -> AsyncIterator[CrashHarness]:
    """崩溃恢复夹具:seed Run → 受控故障点推进 → 恢复 → 断言。"""
    harness = await CrashHarness.create(engine)
    try:
        yield harness
    finally:
        await _cleanup_scenario(engine, harness._ids)


# ---------------------------------------------------------------------------
# 计划原文场景:外部写入生效但响应丢失
# ---------------------------------------------------------------------------


async def test_response_loss_reconciles_without_second_write(
    crash_harness: CrashHarness,
) -> None:
    harness = crash_harness
    harness.set_backend_rounds([harness.write_round()])

    first = await harness.advance_until(AFTER_EXTERNAL_WRITE_BEFORE_RESULT)
    assert first.run.status == RunStatus.WAITING_RECONCILIATION

    recovered = await harness.recover()
    assert recovered.operation is not None
    assert recovered.operation.status == OperationStatus.APPLIED
    assert recovered.run.status == RunStatus.COMPLETED
    # 回查确认外部写入已生效:权威事实在库,恢复链路未产生第二次写入。
    assert harness.external_write_count == 1
    assert recovered.tool_call is not None
    assert recovered.tool_call.status.value == "SUCCEEDED"


# ---------------------------------------------------------------------------
# 七个故障场景
# ---------------------------------------------------------------------------


async def test_interrupted_run_recovers_and_completes(
    crash_harness: CrashHarness,
) -> None:
    """场景 a:执行前进程退出——RUNNING 残留由恢复扫描退回 QUEUED 并推进完成。"""
    harness = crash_harness
    harness.set_backend_rounds([])  # 恢复推进一轮即完成(无 ToolCall)。

    # 模拟"领取后、开始推进前进程被杀":Run 已 RUNNING,Attempt 停在 RUNNING。
    claim = await harness.claim()
    assert claim is not None
    snapshot = await harness.snapshot()
    assert snapshot.run.status == RunStatus.RUNNING
    assert len(snapshot.attempts) == 1

    recovered = await harness.recover()
    assert recovered.run.status == RunStatus.COMPLETED
    assert len(recovered.attempts) == 2
    assert recovered.attempts[1].status == RunAttemptStatus.SUCCEEDED
    assert harness.external_write_count == 0


async def test_executing_operation_exit_reconciles_before_resuming(
    crash_harness: CrashHarness,
) -> None:
    """场景 b:Operation 已 EXECUTING 后进程退出——恢复必须先回查,不得直接推进。"""
    harness = crash_harness
    await harness.seed_pending_write_tool_call()

    claim = await harness.claim()
    assert claim is not None
    begun = await harness.begin_operation()
    assert begun.status == OperationStatus.EXECUTING

    # 恢复扫描发现 EXECUTING Operation:Run 转 WAITING_RECONCILIATION 并回查。
    harness.set_reconcile_verdicts([ReconcileResult.APPLIED])
    recovered = await harness.recover()
    assert recovered.operation is not None
    assert recovered.operation.status == OperationStatus.APPLIED
    assert harness.external_write_count == 0

    # 恢复前的中间快照语义:Run 不可能在 EXECUTING 状态下被直接推进。
    snapshot = await harness.snapshot()
    assert snapshot.run.status != RunStatus.RUNNING


async def test_reconcile_unknown_then_applied_resumes_run(
    crash_harness: CrashHarness,
) -> None:
    """场景 d:回查 UNKNOWN → APPLIED——预算内继续调度,确认后 Run 恢复推进。"""
    harness = crash_harness
    harness.set_backend_rounds([harness.write_round()])

    first = await harness.advance_until(AFTER_EXTERNAL_WRITE_BEFORE_RESULT)
    assert first.run.status == RunStatus.WAITING_RECONCILIATION
    assert first.operation is not None
    assert first.operation.status == OperationStatus.UNKNOWN

    # 第一次回查仍 UNKNOWN:预算内保持 WAITING_RECONCILIATION 并再次调度。
    uncertain = await harness.apply_reconcile(ReconcileResult.UNKNOWN)
    assert uncertain.result is ReconcileResult.UNKNOWN
    assert uncertain.operation_status == OperationStatus.UNKNOWN
    assert uncertain.run_status == RunStatus.WAITING_RECONCILIATION
    assert uncertain.next_step is RunNextStep.RECONCILE
    assert uncertain.budget_exhausted is False

    # 回查适配器确认 APPLIED:Operation APPLIED,Run 恢复推进到 COMPLETED。
    harness.set_reconcile_verdicts([ReconcileResult.APPLIED])
    recovered = await harness.recover()
    assert recovered.operation is not None
    assert recovered.operation.status == OperationStatus.APPLIED
    assert recovered.run.status == RunStatus.COMPLETED
    assert harness.external_write_count == 1


async def test_second_advance_is_blocked_by_state_version_cas(
    crash_harness: CrashHarness,
) -> None:
    """场景 e:同一 Run 已 RUNNING 时,第二个推进者被 state_version CAS 挡住。

    首版删除了租约与 fencing token,``runs.state_version`` CAS 是唯一并发保护;
    本用例验证它仍然阻止重复推进与第二次外部写入。
    """
    harness = crash_harness
    tool_call = await harness.seed_pending_write_tool_call()

    # 第二个推进者读到的版本(领取前):第一个推进者领取后它必须 CAS 失败。
    stale_version = await harness.state_version()

    claim = await harness.claim()
    assert claim is not None
    begun = await harness.begin_operation()
    prepared = await harness.write_handler.prepare(
        normalized_arguments={"listing_id": "L1"}
    )
    await harness.write_handler.execute_prepared(
        tool_call_id=tool_call.id,
        operation_id=begun.operation_id,
        idempotency_key=begun.idempotency_key,
        prepared=prepared,
    )
    assert harness.external_write_count == 1

    # 第二个推进者持有旧 state_version:直接 CAS 必须零行更新。
    assert await harness.try_cas(stale_version) is False
    # 走正常领取路径同样失败:Run 已不在 QUEUED。
    assert await harness.claim() is None
    snapshot = await harness.snapshot()
    assert snapshot.run.status == RunStatus.RUNNING
    assert snapshot.operation is not None
    assert snapshot.operation.status == OperationStatus.EXECUTING
    assert harness.external_write_count == 1


async def test_schema_failure_keeps_applied_write_and_authoritative_fact(
    crash_harness: CrashHarness,
) -> None:
    """场景 f:ToolResult Schema 失败不清除已发生写入——APPLIED 保持、
    INVALID_TOOL_RESULT 置位、retry_allowed=False,权威事实不被改写。"""
    harness = crash_harness
    harness.set_backend_rounds([harness.write_round()])
    harness.set_receipt(status="APPLIED", payload={"bogus": 1})  # 违反 Output Schema。

    snapshot = await harness.advance_until("no-crash")
    assert snapshot.run.status == RunStatus.COMPLETED
    assert snapshot.tool_call is not None

    # PostToolUse 管线语义(同一回执,真实 ToolResultProcessor):输出失败不改分类。
    receipt = ToolExecutionReceipt(
        tool_call_id=snapshot.tool_call.id,
        status="APPLIED",
        payload={"bogus": 1},
        external_reference="ext-1",
    )
    processed = await ToolResultProcessor(output_schema=TEST_OUTPUT_SCHEMA).process(receipt)
    assert processed.operation_outcome == OperationOutcome.APPLIED
    assert processed.tool_call_error == "INVALID_TOOL_RESULT"
    assert processed.retry_allowed is False

    # 权威事实落库:写入已发生(不因输出失败被解释为未执行),不重试、不二次写。
    assert snapshot.operation is not None
    assert snapshot.operation.status == OperationStatus.APPLIED
    assert snapshot.operation.last_receipt is not None
    assert snapshot.operation.last_receipt["outcome"] == "APPLIED"
    assert snapshot.operation.last_receipt["error_code"] == "INVALID_TOOL_RESULT"
    assert snapshot.operation.last_receipt["payload"] == {"bogus": 1}
    assert snapshot.tool_call.status.value == "SUCCEEDED"
    assert harness.external_write_count == 1


async def test_compression_preserves_protected_context(
    crash_harness: CrashHarness,
) -> None:
    """场景 g:Context 压缩不丢 Protected Context——超预算压缩后,Runtime 安全
    规则、当前用户消息、待确认 fingerprint 仍在组装结果中(集成语境复跑)。"""
    harness = crash_harness
    assembler = make_assembler()
    fingerprint = "fp" + "0123456789" * 6 + "cd"
    view = ConversationView(
        messages=tuple(
            ConversationMessage(
                sequence_no=n,
                role=MessageType.USER if n % 2 == 1 else MessageType.ASSISTANT,
                content=f"history message {n}: " + "h" * 100,
                status=AssistantMessageStatus.COMPLETED if n % 2 == 0 else None,
            )
            for n in range(1, 7)
        ),
        memories=(
            MemoryItem(memory_id="mem-low", content="stale memory: " + "m" * 70, relevance=0.1),
        ),
        tool_result_projections=(
            ToolResultProjection(
                call_id="call-old",
                tool_name="search_products",
                content="old tool result: " + "t" * 70,
                ordinal=1,
            ),
        ),
        pending_confirmation=PendingConfirmationView(
            fingerprint=fingerprint,
            tool_name=harness.write_tool_key,
            argument_summary="update price for SKU A-1",
        ),
        current_user_message=ConversationMessage(
            sequence_no=7,
            role=MessageType.USER,
            content="current intent: confirm the price update",
        ),
    )

    request: AssembledContext = await assembler.build(view, input_budget_tokens=800)

    # 压缩确实发生:历史被 Summary 替代(降级只发生在非 Protected 段)。
    assert request.session_summary is not None
    assert request.session_summary.covered_to_sequence < 7
    assert request.session_summary.provider_role != "system"
    # Protected 三项在压缩后仍在。
    assert request.contains("runtime safety rules")
    assert request.contains("current intent: confirm the price update")
    assert request.contains(fingerprint)
