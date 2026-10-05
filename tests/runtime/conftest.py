"""Execution/Events 测试共享夹具(计划 02 Task 3)。

提供:
- ``db_session``:与 ``engine`` 同库的 AsyncSession,测试结束回滚未提交数据;
- ``seeded_workspace``:run 外键链所需的最小身份与配置种子行;
- ``seeded_session`` / ``seeded_run``:经 Repository 创建的外键依赖行;
- ``run_repository`` / ``events_repository``:与 ``db_session`` 同事务的 Repository;
- ``event_writer``:绑定种子 workspace 的 AgentEventWriter;
- ``run_service``:绑定种子 workspace 的 RunService。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.events.writer import AgentEventWriter
from runtime.execution.run_service import RunService
from runtime.persistence.base import MembershipRole, ModelBackendType
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.execution import Run
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import Session
from runtime.persistence.repositories.events import EventRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


@dataclass(frozen=True)
class WorkspaceSeed:
    """Run 外键依赖的最小种子行(与 persistence 测试同构)。"""

    workspace: Workspace
    user: User
    membership: WorkspaceMembership
    agent: Agent
    agent_version: AgentVersion
    model_config_version: ModelConfigVersion


@pytest.fixture()
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """每个测试一个独立 Session;结束时不提交,回滚以保证测试间隔离。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


@pytest.fixture()
async def seeded_workspace(db_session: AsyncSession) -> WorkspaceSeed:
    """创建 run 外键链所需的最小行。"""
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
    return WorkspaceSeed(
        workspace=workspace,
        user=user,
        membership=membership,
        agent=agent,
        agent_version=agent_version,
        model_config_version=model_config_version,
    )


@pytest.fixture()
async def seeded_session(
    seeded_workspace: WorkspaceSeed, db_session: AsyncSession
) -> Session:
    return await SessionRepository(db_session).create(
        workspace_id=seeded_workspace.workspace.id,
        agent_id=seeded_workspace.agent.id,
        agent_version_id=seeded_workspace.agent_version.id,
        created_by_actor_id=seeded_workspace.user.id,
        created_by_membership_id=seeded_workspace.membership.id,
    )


@pytest.fixture()
def run_repository(db_session: AsyncSession) -> RunRepository:
    return RunRepository(db_session)


@pytest.fixture()
def events_repository(db_session: AsyncSession) -> EventRepository:
    return EventRepository(db_session)


@pytest.fixture()
async def seeded_run(
    db_session: AsyncSession,
    run_repository: RunRepository,
    seeded_workspace: WorkspaceSeed,
    seeded_session: Session,
) -> Run:
    run = await run_repository.create(
        workspace_id=seeded_workspace.workspace.id,
        session_id=seeded_session.id,
        created_by_actor_id=seeded_workspace.user.id,
        created_by_membership_id=seeded_workspace.membership.id,
        agent_version_id=seeded_workspace.agent_version.id,
        model_config_version_id=seeded_workspace.model_config_version.id,
    )
    # 载入 server default(state_version/next_event_sequence),供 CAS 断言。
    await db_session.refresh(run)
    return run


@pytest.fixture()
def event_writer(db_session: AsyncSession, seeded_workspace: WorkspaceSeed) -> AgentEventWriter:
    """绑定种子 workspace 的事件写入器;与 db_session 共享事务。"""
    return AgentEventWriter(session=db_session, workspace_id=seeded_workspace.workspace.id)


@pytest.fixture()
def run_service(db_session: AsyncSession, seeded_workspace: WorkspaceSeed) -> RunService:
    """绑定种子 workspace 的 Run 状态迁移服务;与 db_session 共享事务。"""
    return RunService(session=db_session, workspace_id=seeded_workspace.workspace.id)
