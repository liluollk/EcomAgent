"""Repository 测试夹具(计划 01 Task 4)。

提供:
- ``db_session``:与 ``engine`` 同库的 AsyncSession,测试结束回滚未提交数据;
- 最小种子数据:workspace/user/membership/agent/agent_version/model 配置版本直接
  以 ORM 插入(遵循模型必填列),session 与 run 通过被测 Repository 创建;
- 六个 Repository 实例(与 ``db_session`` 共享同一事务)。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
from runtime.persistence.repositories.requests import RequestRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


@dataclass(frozen=True)
class WorkspaceSeed:
    """最小身份与配置种子行。"""

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
    """创建最小 workspace/user/membership/agent/版本快照依赖。"""
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
def session_repository(db_session: AsyncSession) -> SessionRepository:
    return SessionRepository(db_session)


@pytest.fixture()
def run_repository(db_session: AsyncSession) -> RunRepository:
    return RunRepository(db_session)


@pytest.fixture()
def requests_repository(db_session: AsyncSession) -> RequestRepository:
    return RequestRepository(db_session)


@pytest.fixture()
def events_repository(db_session: AsyncSession) -> EventRepository:
    return EventRepository(db_session)


@pytest.fixture()
async def seeded_session(
    session_repository: SessionRepository, seeded_workspace: WorkspaceSeed
) -> Session:
    return await session_repository.create(
        workspace_id=seeded_workspace.workspace.id,
        agent_id=seeded_workspace.agent.id,
        agent_version_id=seeded_workspace.agent_version.id,
        created_by_actor_id=seeded_workspace.user.id,
        created_by_membership_id=seeded_workspace.membership.id,
    )


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
    # 载入 server default(state_version/next_event_sequence),供 CAS 测试断言。
    await db_session.refresh(run)
    return run
