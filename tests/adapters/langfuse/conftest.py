"""Langfuse Trace Outbox 测试共享夹具(计划 04 Task 8)。

提供:
- ``db_session`` / ``seeded_workspace``:最小身份与 Run 外键链种子;
- ``completed_run`` / ``run_with_events``:计划原文 ``completed_run`` 及带
  已落库事件的 Run(RUN_STARTED + TOOL_RESULT_RECORDED);
- ``fake_langfuse_client``:内存版 LangfuseClient,不发真实网络;
- ``trace_exporter`` / ``event_with_secret``:计划原文引用;
- ``outbox_worker`` / ``trace_outbox``:注入 handler 的 Worker 与
  ``trace_outbox.process_with_failure(run_id)`` 驱动器。
"""

import copy
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.langfuse.exporter import TraceEventSnapshot, TraceExporter
from adapters.langfuse.outbox_handler import LangfuseOutboxHandler
from runtime.integrations.outbox_worker import OutboxWorker
from runtime.observability.trace_outbox import enqueue_trace_export
from runtime.persistence.base import MembershipRole, ModelBackendType, OutboxKind, RunStatus
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.events import AgentEvent
from runtime.persistence.models.execution import Run
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.integrations import IntegrationOutbox
from runtime.persistence.models.interaction import Session
from runtime.persistence.repositories.runs import RunRepository


class FakeLangfuseClient:
    """内存版 LangfuseClient:记录 ingest 批次并可注入失败;不发真实网络。"""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self._fail_with = fail_with

    async def ingest(self, batch: Sequence[Mapping[str, Any]]) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.batches.append(copy.deepcopy([dict(item) for item in batch]))


@dataclass
class RunRef:
    """计划原文 ``completed_run.id`` / ``completed_run.reload_status()`` 引用。"""

    run: Run
    session: AsyncSession

    @property
    def id(self) -> uuid.UUID:
        return self.run.id

    async def reload_status(self) -> RunStatus:
        await self.session.refresh(self.run)
        return self.run.status


@dataclass
class TraceOutboxDriver:
    """计划原文 ``trace_outbox.process_with_failure(run_id)`` 引用。

    先确保该 Run 存在一条 TRACE_EXPORT Outbox 行,再经 OutboxWorker 走失败
    路径(重试/DEAD 语义由 Worker 统一保证)。
    """

    session: AsyncSession
    worker: OutboxWorker

    async def process_with_failure(self, run_id: uuid.UUID) -> bool:
        row = await self.session.scalar(
            select(IntegrationOutbox).where(
                IntegrationOutbox.kind == OutboxKind.TRACE_EXPORT,
                IntegrationOutbox.aggregate_id == str(run_id),
            )
        )
        if row is None:
            run = await self.session.get(Run, run_id)
            assert run is not None
            await enqueue_trace_export(self.session, workspace_id=run.workspace_id, run_id=run_id)
        return await self.worker.process_with_failure("TRACE_EXPORT")


@pytest.fixture()
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


@pytest.fixture()
async def seeded_workspace(db_session: AsyncSession) -> Any:
    """workspace/user/membership 与 Run 外键链的最小种子。"""
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
    # 模型间无 relationship() 声明,SQLAlchemy 不保证跨表插入顺序;
    # 按外键依赖分批 flush,保证被引用行先落库。
    db_session.add_all([workspace, user, agent])
    await db_session.flush()
    db_session.add_all([membership, agent_version, model_connection])
    await db_session.flush()
    db_session.add(model_config_version)
    await db_session.flush()
    session = Session(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        agent_id=agent.id,
        created_by_actor_id=user.id,
        created_by_membership_id=membership.id,
    )
    db_session.add(session)
    await db_session.flush()
    return {
        "workspace": workspace,
        "user": user,
        "membership": membership,
        "agent_version": agent_version,
        "model_config_version": model_config_version,
        "session": session,
    }


async def _create_completed_run(
    db_session: AsyncSession, seeded_workspace: Any
) -> RunRef:
    """与 Outbox 同 workspace 的 COMPLETED Run;集成失败不得影响它。"""
    run = await RunRepository(db_session).create(
        workspace_id=seeded_workspace["workspace"].id,
        session_id=seeded_workspace["session"].id,
        created_by_actor_id=seeded_workspace["user"].id,
        created_by_membership_id=seeded_workspace["membership"].id,
        agent_version_id=seeded_workspace["agent_version"].id,
        model_config_version_id=seeded_workspace["model_config_version"].id,
        status=RunStatus.COMPLETED,
    )
    run.completed_at = datetime.now(UTC)
    await db_session.flush()
    return RunRef(run=run, session=db_session)


@pytest.fixture()
async def completed_run(
    db_session: AsyncSession, seeded_workspace: Any
) -> RunRef:
    return await _create_completed_run(db_session, seeded_workspace)


@pytest.fixture()
async def run_with_events(
    db_session: AsyncSession, seeded_workspace: Any
) -> RunRef:
    """COMPLETED Run + 两条已落库事件;事件 payload 故意携带密钥形态值。"""
    run_ref = await _create_completed_run(db_session, seeded_workspace)
    run = run_ref.run
    db_session.add_all(
        [
            AgentEvent(
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                run_id=run.id,
                sequence_no=1,
                event_type="RUN_STARTED",
                payload={"trigger": "manual"},
            ),
            AgentEvent(
                workspace_id=run.workspace_id,
                session_id=run.session_id,
                run_id=run.id,
                sequence_no=2,
                event_type="TOOL_RESULT_RECORDED",
                payload={"tool": "order.search", "api_key": "sk-testsecret12345"},
            ),
        ]
    )
    await db_session.flush()
    return run_ref


@pytest.fixture()
def fake_langfuse_client() -> FakeLangfuseClient:
    return FakeLangfuseClient()


@pytest.fixture()
def failing_langfuse_client() -> FakeLangfuseClient:
    return FakeLangfuseClient(fail_with=RuntimeError("langfuse down"))


@pytest.fixture()
def trace_exporter(fake_langfuse_client: FakeLangfuseClient) -> TraceExporter:
    return TraceExporter(fake_langfuse_client)


@pytest.fixture()
def event_with_secret() -> Callable[[str], TraceEventSnapshot]:
    """计划原文 ``event_with_secret("sk-sensitive")``:Secret 藏在多层嵌套里。"""

    def _factory(secret: str) -> TraceEventSnapshot:
        return TraceEventSnapshot(
            event_type="TOOL_RESULT",
            run_id="run-1",
            session_id="sess-1",
            sequence_no=3,
            payload={
                "messages": [{"role": "user", "content": f"call with {secret}"}],
                "api_key": secret,
                "nested": {"authorization": f"Bearer {secret}", "run": "run-1"},
            },
            tool_call_id="tool-9",
        )

    return _factory


@pytest.fixture()
def langfuse_handler(
    db_session: AsyncSession, fake_langfuse_client: FakeLangfuseClient
) -> LangfuseOutboxHandler:
    return LangfuseOutboxHandler(db_session, fake_langfuse_client)


@pytest.fixture()
def outbox_worker(
    db_session: AsyncSession, langfuse_handler: LangfuseOutboxHandler
) -> OutboxWorker:
    return OutboxWorker(db_session, handler=langfuse_handler)


@pytest.fixture()
def trace_outbox(db_session: AsyncSession, outbox_worker: OutboxWorker) -> TraceOutboxDriver:
    return TraceOutboxDriver(session=db_session, worker=outbox_worker)
