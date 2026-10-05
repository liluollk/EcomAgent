"""Audit 测试夹具(计划 01 Task 7)。

提供:
- ``db_session``:与 ``engine`` 同库的 AsyncSession,测试结束回滚未提交数据;
- ``seeded_workspace``:audit_events 外键依赖(workspace/user)与 run 外键链
  所需的最小身份与配置种子行;
- ``seeded_run``:审计 run_id 关联所需的外键行(session + run,经 Repository 创建);
- ``audit_service``:绑定 workspace+actor 的只追加审计服务,与 db_session 同事务;
- ``audit_event``:预置一条审计事件,供 update 拒绝等测试使用。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.audit.models import AuditEventView
from runtime.audit.service import AuditService
from runtime.persistence.base import MembershipRole, ModelBackendType
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.execution import Run
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


@dataclass(frozen=True)
class WorkspaceSeed:
    """审计外键依赖的最小种子行(与 persistence 测试同构)。"""

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
    """创建 audit_events 外键依赖与 run 外键链所需的最小行。"""
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
async def seeded_run(
    db_session: AsyncSession, seeded_workspace: WorkspaceSeed
) -> Run:
    """审计 run_id 外键依赖:经 Repository 创建最小 session 与 run。"""
    session_row = await SessionRepository(db_session).create(
        workspace_id=seeded_workspace.workspace.id,
        agent_id=seeded_workspace.agent.id,
        agent_version_id=seeded_workspace.agent_version.id,
        created_by_actor_id=seeded_workspace.user.id,
        created_by_membership_id=seeded_workspace.membership.id,
    )
    return await RunRepository(db_session).create(
        workspace_id=seeded_workspace.workspace.id,
        session_id=session_row.id,
        created_by_actor_id=seeded_workspace.user.id,
        created_by_membership_id=seeded_workspace.membership.id,
        agent_version_id=seeded_workspace.agent_version.id,
        model_config_version_id=seeded_workspace.model_config_version.id,
    )


@pytest.fixture()
def audit_service(db_session: AsyncSession, seeded_workspace: WorkspaceSeed) -> AuditService:
    """绑定种子 workspace 与 actor 的只追加审计服务;与 db_session 共享事务。"""
    return AuditService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )


@pytest.fixture()
async def audit_event(audit_service: AuditService) -> AuditEventView:
    """预置一条审计事件,供 update 拒绝与读取测试使用。"""
    return await audit_service.append(
        action="MODEL_SECRET_UPDATED",
        payload={"name": "gpt-test"},
        target_type="model_config",
        target_id="gpt-test",
    )
