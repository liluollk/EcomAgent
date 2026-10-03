"""Interaction 测试夹具(计划 02 Task 7)。

提供:
- ``db_session``:与 ``engine`` 同库的 AsyncSession,测试结束回滚未提交数据;
- ``seeded_workspace`` / ``seeded_run``:请求外键链所需的最小身份与配置种子行;
- ``seeded_tool_call``:confirmation_requests.tool_call_id 外键链所需的
  tool_definitions(GLOBAL)→ tool_versions → messages → tool_calls 行;
- ``audit_service``:绑定种子 workspace 的只追加审计服务(供断言读取);
- ``input_service`` / ``confirmation_service`` / ``expire_service``:与
  ``db_session`` 同事务的被测服务;
- ``pending_input`` / ``expired_input`` / ``pending_confirmation`` /
  ``accepted_confirmation``:计划原文测试所需的请求夹具,以 Handle 形式暴露
  ``id`` 与 ``reload_status()``。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.audit.service import AuditService
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interaction.expiry import ExpireService
from runtime.interaction.input_service import InputService
from runtime.persistence.base import (
    ConfigScope,
    MembershipRole,
    MessageRole,
    MessageStatus,
    ModelBackendType,
    ToolEffect,
)
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
    ToolDefinition,
    ToolVersion,
)
from runtime.persistence.models.execution import Run, ToolCall
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
)
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


@dataclass(frozen=True)
class WorkspaceSeed:
    """Request 外键依赖的最小种子行(与 persistence 测试同构)。"""

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
    """创建请求外键链所需的最小身份与配置种子行。"""
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
    db_session: AsyncSession,
    seeded_workspace: WorkspaceSeed,
) -> Run:
    """请求行外键依赖:经 Repository 创建最小 session 与 run。"""
    session_row = await SessionRepository(db_session).create(
        workspace_id=seeded_workspace.workspace.id,
        agent_id=seeded_workspace.agent.id,
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
async def seeded_tool_call(
    db_session: AsyncSession, seeded_workspace: WorkspaceSeed, seeded_run: Run
) -> ToolCall:
    """confirmation_requests.tool_call_id 外键链:GLOBAL tool + message + tool_call。"""
    tool_definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        workspace_id=None,
        stable_key=f"tool-{uuid.uuid4()}",
        name="update_listing_price",
    )
    tool_version = ToolVersion(
        id=uuid.uuid4(),
        workspace_id=None,
        tool_id=tool_definition.id,
        version=1,
        effect=ToolEffect.WRITE,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        schema_hash="schema-hash-1",
    )
    message = Message(
        workspace_id=seeded_workspace.workspace.id,
        session_id=seeded_run.session_id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETED,
        content="call update_listing_price",
        sequence_no=1,
    )
    await db_session.flush()
    db_session.add_all([tool_definition, tool_version, message])
    await db_session.flush()
    tool_call = ToolCall(
        workspace_id=seeded_workspace.workspace.id,
        run_id=seeded_run.id,
        message_id=message.id,
        tool_version_id=tool_version.id,
        ordinal=1,
        raw_arguments={"listing_id": "L1"},
        normalized_arguments=None,
    )
    db_session.add(tool_call)
    await db_session.flush()
    return tool_call


@pytest.fixture()
def audit_service(db_session: AsyncSession, seeded_workspace: WorkspaceSeed) -> AuditService:
    """绑定种子 workspace 与 actor 的审计服务;供测试断言读取审计行。"""
    return AuditService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )


@pytest.fixture()
def input_service(
    db_session: AsyncSession, seeded_workspace: WorkspaceSeed
) -> InputService:
    """绑定种子 workspace 的 Input 服务;与 db_session 共享事务。"""
    return InputService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )


@pytest.fixture()
def confirmation_service(
    db_session: AsyncSession, seeded_workspace: WorkspaceSeed
) -> ConfirmationService:
    """绑定种子 workspace 的 Confirmation 服务;与 db_session 共享事务。"""
    return ConfirmationService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )


@pytest.fixture()
def expire_service(
    db_session: AsyncSession, seeded_workspace: WorkspaceSeed
) -> ExpireService:
    """绑定种子 workspace 的过期清扫服务;审计归属种子 actor。"""
    return ExpireService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )


class InputRequestHandle:
    """计划原文 fixture 的最小包装:暴露 ``id`` 与 ``reload_status()``。"""

    def __init__(self, session: AsyncSession, request_id: uuid.UUID) -> None:
        self._session = session
        self.id = request_id

    async def reload_status(self) -> Any:
        row = (
            await self._session.scalars(
                select(InputRequest)
                .where(InputRequest.id == self.id)
                .execution_options(populate_existing=True)
            )
        ).one()
        return row.status


class ConfirmationRequestHandle:
    """计划原文 fixture 的最小包装:暴露 ``id`` 与 ``reload_status()``。"""

    def __init__(self, session: AsyncSession, request_id: uuid.UUID) -> None:
        self._session = session
        self.id = request_id

    async def reload_status(self) -> Any:
        row = (
            await self._session.scalars(
                select(ConfirmationRequest)
                .where(ConfirmationRequest.id == self.id)
                .execution_options(populate_existing=True)
            )
        ).one()
        return row.status


def _expires_in(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


@pytest.fixture()
async def pending_input(
    db_session: AsyncSession, input_service: InputService, seeded_run: Run
) -> InputRequestHandle:
    """PENDING Input Request:field_paths 只声明 ``store_id``。"""
    request = await input_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        field_paths=["store_id"],
        question="请补充门店 ID",
        response_schema={"type": "object"},
        expires_at=_expires_in(5),
    )
    return InputRequestHandle(db_session, request.id)


@pytest.fixture()
async def expired_input(
    db_session: AsyncSession, input_service: InputService, seeded_run: Run
) -> InputRequestHandle:
    """已过期但仍处 PENDING 的 Input Request(sweep 前状态由条件更新兜底)。"""
    request = await input_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        field_paths=["store_id"],
        question="请补充门店 ID",
        response_schema={"type": "object"},
        expires_at=_expires_in(-1),
    )
    return InputRequestHandle(db_session, request.id)


@pytest.fixture()
async def pending_confirmation(
    db_session: AsyncSession,
    confirmation_service: ConfirmationService,
    seeded_run: Run,
    seeded_tool_call: ToolCall,
) -> ConfirmationRequestHandle:
    """PENDING Confirmation Request:资源版本为 ``{"listing": 1}``。"""
    request = await confirmation_service.create(
        session_id=seeded_run.session_id,
        run_id=seeded_run.id,
        tool_call_id=seeded_tool_call.id,
        normalized_arguments={"listing_id": "L1", "price": "1.00"},
        resource_versions={"listing": 1},
        expires_at=_expires_in(5),
    )
    return ConfirmationRequestHandle(db_session, request.id)


@pytest.fixture()
async def accepted_confirmation(
    confirmation_service: ConfirmationService,
    pending_confirmation: ConfirmationRequestHandle,
) -> ConfirmationRequestHandle:
    """已被接受(ACCEPTED)的 Confirmation Request。"""
    await confirmation_service.accept(pending_confirmation.id)
    return pending_confirmation
