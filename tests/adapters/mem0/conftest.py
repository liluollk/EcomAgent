"""Mem0 Outbox 测试共享夹具(计划 04 Task 7)。

提供:
- ``db_session`` / ``seeded_workspace``:最小身份与 Run 外键链种子;
- ``memory_service`` / ``memory_candidate``:产生 MEMORY_UPSERT/MEMORY_DELETE Outbox;
- ``fake_mem0_client`` / ``mem0_handler``:Mem0Client fake 与被测 handler;
- ``outbox_worker``:注入 fake handler 的 OutboxWorker;
- ``memory_upsert_outbox`` / ``expired_outbox`` / ``completed_run``:计划原文夹具。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fixtures.mem0_fakes import FakeMem0Client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.integrations.outbox_worker import OutboxWorker
from runtime.memory.service import MemoryCandidate, MemoryService
from runtime.persistence.base import MembershipRole, ModelBackendType, OutboxKind, RunStatus
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.execution import Run
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.integrations import IntegrationOutbox, MemoryRecord
from runtime.persistence.models.interaction import Session
from runtime.persistence.repositories.runs import RunRepository


@dataclass
class RunRef:
    """计划原文 ``completed_run.reload_status()`` 引用。"""

    run: Run
    session: AsyncSession

    async def reload_status(self) -> RunStatus:
        await self.session.refresh(self.run)
        return self.run.status


@dataclass(frozen=True)
class ExpiredOutboxRef:
    """过期租约引用:``lease_token`` 是回收前的旧 token(计划原文断言)。"""

    id: uuid.UUID
    lease_token: uuid.UUID


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


@pytest.fixture()
def memory_service(db_session: AsyncSession, seeded_workspace: Any) -> MemoryService:
    return MemoryService(
        db_session,
        workspace_id=seeded_workspace["workspace"].id,
        actor_id=seeded_workspace["user"].id,
    )


@pytest.fixture()
def memory_candidate(seeded_workspace: Any) -> Any:
    actor_id = seeded_workspace["user"].id

    def _candidate(**overrides: Any) -> MemoryCandidate:
        values: dict[str, Any] = {
            "kind": "preference",
            "content": "prefer short replies",
            "subject_key": f"user-{uuid.uuid4()}",
            "actor_id": actor_id,
        }
        values.update(overrides)
        return MemoryCandidate(**values)

    return _candidate


@pytest.fixture()
def fake_mem0_client() -> FakeMem0Client:
    return FakeMem0Client()


@pytest.fixture()
def mem0_handler(
    db_session: AsyncSession, fake_mem0_client: FakeMem0Client
) -> Any:
    from adapters.mem0.outbox_handler import Mem0OutboxHandler

    return Mem0OutboxHandler(db_session, fake_mem0_client)


@pytest.fixture()
def outbox_worker(db_session: AsyncSession, mem0_handler: Any) -> OutboxWorker:
    return OutboxWorker(db_session, handler=mem0_handler)


@pytest.fixture()
async def memory_upsert_outbox(
    db_session: AsyncSession,
    seeded_workspace: Any,
    memory_service: MemoryService,
    memory_candidate: Any,
) -> IntegrationOutbox:
    """经 memory_service.accept 产生的 PENDING MEMORY_UPSERT Outbox 行。"""
    assert memory_service.accept(memory_candidate()) is True
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace["workspace"].id
            )
        )
    ).one()
    outbox = (
        await db_session.scalars(
            select(IntegrationOutbox).where(
                IntegrationOutbox.workspace_id == seeded_workspace["workspace"].id,
                IntegrationOutbox.kind == OutboxKind.MEMORY_UPSERT,
                IntegrationOutbox.aggregate_id == str(record.id),
            )
        )
    ).one()
    return outbox


@pytest.fixture()
async def expired_outbox(
    db_session: AsyncSession,
    outbox_worker: OutboxWorker,
    memory_upsert_outbox: IntegrationOutbox,
) -> ExpiredOutboxRef:
    """已被 worker-a 领取但租约已过期的 Outbox(模拟失租 Worker)。

    返回回收前的 id 与旧 lease token 快照;ORM 行会被 worker-b 的重新
    领取原地改写,断言必须使用快照。
    """
    lease = await outbox_worker.claim("worker-a")
    assert lease is not None
    memory_upsert_outbox.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()
    return ExpiredOutboxRef(id=memory_upsert_outbox.id, lease_token=lease.lease_token)


@pytest.fixture()
async def completed_run(db_session: AsyncSession, seeded_workspace: Any) -> RunRef:
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
