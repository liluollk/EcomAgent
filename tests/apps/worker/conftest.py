"""Worker 生命周期测试共享夹具(计划 04 Task 5)。

提供:
- ``harness_factory`` / ``worker_host``:WorkerHostHarness 构造入口;
- ``worker_seed``:数据库集成测试的提交式最小种子(身份链 + Session + Run)。
  Worker 与租约网关使用独立短事务,种子必须已提交;测试结束按外键逆序清理。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from fixtures.worker_harness import WorkerHostHarness
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.persistence.base import MembershipRole, ModelBackendType
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
)
from runtime.persistence.models.execution import Run, RunAttempt, RunJob
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import Session
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


@dataclass(frozen=True)
class WorkerSeed:
    """数据库集成测试的最小种子标识。"""

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID


@pytest.fixture()
def harness_factory() -> Callable[..., WorkerHostHarness]:
    def _make(**kwargs: Any) -> WorkerHostHarness:
        return WorkerHostHarness(**kwargs)

    return _make


@pytest.fixture()
def worker_host(harness_factory: Callable[..., WorkerHostHarness]) -> WorkerHostHarness:
    """快节奏默认夹具:10ms 心跳 / 5ms 轮询 / 250ms 强制停止超时。"""
    return harness_factory()


@pytest.fixture()
async def worker_seed(engine: AsyncEngine) -> AsyncIterator[WorkerSeed]:
    """提交式种子:身份链 + Session + Run;结束按外键依赖逆序清理。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
            user = User(
                id=uuid.uuid4(),
                login=f"worker-{uuid.uuid4()}",
                password_hash="test-hash",
            )
            agent = Agent(
                id=uuid.uuid4(),
                stable_key=f"agent-{uuid.uuid4()}",
                name="worker-test-agent",
            )
            session.add_all([workspace, user, agent])
            await session.flush()
            membership = WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.OPERATOR,
            )
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=1,
                definition_json={},
                definition_hash="test-hash",
            )
            connection = ModelConnection(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                stable_key=f"model-{uuid.uuid4()}",
                name="worker-test-model",
            )
            session.add_all([membership, agent_version, connection])
            await session.flush()
            config_version = ModelConfigVersion(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                model_connection_id=connection.id,
                version=1,
                backend_type=ModelBackendType.OPENAI,
                base_url="https://api.example.com/v1",
                model="gpt-test",
                non_secret_options={},
                config_hash="test-hash",
            )
            session.add(config_version)
            await session.flush()
            agent_session = await SessionRepository(session).create(
                workspace_id=workspace.id,
                agent_id=agent.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
            )
            run = await RunRepository(session).create(
                workspace_id=workspace.id,
                session_id=agent_session.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
                agent_version_id=agent_version.id,
                model_config_version_id=config_version.id,
            )
        seed = WorkerSeed(
            workspace_id=workspace.id,
            user_id=user.id,
            agent_id=agent.id,
            session_id=agent_session.id,
            run_id=run.id,
        )
        yield seed
    finally:
        # 按外键依赖逆序清理本 workspace 的种子行(含 Worker 提交的 Job/Attempt)。
        workspace_id = seed.workspace_id
        agent_id = seed.agent_id
        async with session.begin():
            for statement in (
                sa.delete(RunJob).where(RunJob.workspace_id == workspace_id),
                sa.delete(RunAttempt).where(RunAttempt.workspace_id == workspace_id),
                sa.delete(Run).where(Run.workspace_id == workspace_id),
                sa.delete(Session).where(Session.workspace_id == workspace_id),
                sa.delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id
                ),
                sa.delete(ModelConfigVersion).where(
                    ModelConfigVersion.workspace_id == workspace_id
                ),
                sa.delete(ModelConnection).where(
                    ModelConnection.workspace_id == workspace_id
                ),
                sa.delete(AgentVersion).where(AgentVersion.agent_id == agent_id),
                sa.delete(Agent).where(Agent.id == agent_id),
                sa.delete(Workspace).where(Workspace.id == workspace_id),
                sa.delete(User).where(User.id == seed.user_id),
            ):
                await session.execute(statement)
        await session.close()
