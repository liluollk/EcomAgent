"""Session/Run/Request 命令 API 测试(计划 04 Task 3;架构文档《Runtime执行状态与恢复设计》§4、§5、§6、§9)。

覆盖计划原文测试(活动 Run 阻止新用户消息)与补充语义:
- 创建 Session 绑定当前 actor;提交用户消息创建 Run(QUEUED),由执行器推进;
- 私有 Session:非创建者/非成员读取一律 404,不泄漏存在性;
- 取消 Run 经 transitions 合法路径迁移 CANCELLED,终态再取消 409;
- Input Request 单次回答:未声明字段 422 UNDECLARED_FIELD,已处理 409 REQUEST_NOT_PENDING,
  成功后 Run 回到 QUEUED(等价 Runtime 恢复推进的最小事务,不调用推进循环);
- Confirmation Request 单次 accept/reject,二次 409;
- Run 快照(status/messages/tool_calls/events cursor);跨工作区资源 404。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from apps.api.main import create_app
from apps.api.schemas.sessions import CreateSessionRequest
from runtime.access.tokens import issue_token
from runtime.execution.run_service import RunService
from runtime.interaction.confirmation_service import ConfirmationService
from runtime.interaction.input_service import InputService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    AccountStatus,
    ConfigScope,
    ConfigVersionStatus,
    MembershipRole,
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
    WorkspaceAgentBinding,
)
from runtime.persistence.models.events import AgentEvent, AuditEvent
from runtime.persistence.models.execution import Run, RunAttempt, ToolCall
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
    Session,
)


@dataclass(frozen=True)
class SeededWorkspace:
    """两个成员的 Workspace 种子与最小能力版本(Agent/Model/Tool)。"""

    workspace: Workspace
    owner: User
    operator: User
    non_member: User
    owner_membership: WorkspaceMembership
    agent: Agent
    agent_version: AgentVersion
    agent_binding: WorkspaceAgentBinding
    model_config_version: ModelConfigVersion
    tool_version: ToolVersion

    @property
    def id(self) -> UUID:
        return self.workspace.id

    @property
    def agent_id(self) -> UUID:
        return self.agent.id


@dataclass(frozen=True)
class CreatedRun:
    """一次 POST messages 创建的消息与 Run。"""

    session_id: UUID
    message_id: UUID
    run_id: UUID


async def _cleanup(session: AsyncSession, workspace_id: UUID, extra: SeededWorkspace) -> None:
    """按外键顺序删除种子与其派生行;已删除的行重复删除无副作用。

    runs.trigger_message_id 与 messages.run_id 相互引用:先解除两端引用再删除;
    sessions 依赖 agents/memberships,故全局配置行最后删除。
    """
    await session.execute(sa.delete(AgentEvent).where(AgentEvent.workspace_id == workspace_id))
    await session.execute(sa.delete(AuditEvent).where(AuditEvent.workspace_id == workspace_id))
    await session.execute(sa.delete(RunAttempt).where(RunAttempt.workspace_id == workspace_id))
    await session.execute(sa.delete(InputRequest).where(InputRequest.workspace_id == workspace_id))
    await session.execute(
        sa.delete(ConfirmationRequest).where(ConfirmationRequest.workspace_id == workspace_id)
    )
    await session.execute(sa.delete(ToolCall).where(ToolCall.workspace_id == workspace_id))
    await session.execute(
        sa.update(Message).where(Message.workspace_id == workspace_id).values(run_id=None)
    )
    await session.execute(
        sa.update(Run).where(Run.workspace_id == workspace_id).values(trigger_message_id=None)
    )
    await session.execute(sa.delete(Run).where(Run.workspace_id == workspace_id))
    await session.execute(sa.delete(Message).where(Message.workspace_id == workspace_id))
    await session.execute(sa.delete(Session).where(Session.workspace_id == workspace_id))
    await session.execute(
        sa.delete(ModelConfigVersion).where(ModelConfigVersion.workspace_id == workspace_id)
    )
    await session.execute(
        sa.delete(ModelConnection).where(ModelConnection.workspace_id == workspace_id)
    )
    await session.execute(sa.delete(ToolVersion).where(ToolVersion.id == extra.tool_version.id))
    await session.execute(
        sa.delete(ToolDefinition).where(ToolDefinition.id == extra.tool_version.tool_id)
    )
    await session.execute(
        sa.delete(WorkspaceAgentBinding).where(
            WorkspaceAgentBinding.workspace_id == workspace_id
        )
    )
    await session.execute(sa.delete(AgentVersion).where(AgentVersion.agent_id == extra.agent.id))
    await session.execute(sa.delete(Agent).where(Agent.id == extra.agent.id))
    await session.execute(
        sa.delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id)
    )
    await session.execute(sa.delete(Workspace).where(Workspace.id == workspace_id))
    await session.execute(
        sa.delete(User).where(
            User.id.in_([extra.owner.id, extra.operator.id, extra.non_member.id])
        )
    )
    await session.commit()


@pytest.fixture()
async def workspace(engine: AsyncEngine) -> AsyncIterator[SeededWorkspace]:
    """创建双成员 Workspace 与最小能力版本种子;结束后按外键顺序清理。"""
    owner = User(
        id=uuid.uuid4(),
        login=f"owner-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    operator = User(
        id=uuid.uuid4(),
        login=f"operator-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    non_member = User(
        id=uuid.uuid4(),
        login=f"outsider-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    ws = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:8]}")
    owner_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        user_id=owner.id,
        role=MembershipRole.WORKSPACE_OWNER,
        status=AccountStatus.ACTIVE,
    )
    agent = Agent(id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4().hex[:12]}", name="test agent")
    agent_version = AgentVersion(
        id=uuid.uuid4(),
        agent_id=agent.id,
        version=1,
        status=ConfigVersionStatus.PUBLISHED,
        definition_json={"role": "test"},
        definition_hash="test-definition-hash",
    )
    connection = ModelConnection(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        stable_key=f"conn-{uuid.uuid4().hex[:12]}",
        name="test model",
    )
    model_config_version = ModelConfigVersion(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        model_connection_id=connection.id,
        version=1,
        backend_type=ModelBackendType.OPENAI,
        base_url="https://model.invalid/v1",
        model="test-model",
        non_secret_options={},
        config_hash="test-config-hash",
    )
    tool_definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        workspace_id=None,
        stable_key=f"tool-{uuid.uuid4().hex[:12]}",
        name="test tool",
    )
    tool_version = ToolVersion(
        id=uuid.uuid4(),
        workspace_id=None,
        tool_id=tool_definition.id,
        version=1,
        effect=ToolEffect.WRITE,
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        schema_hash="test-schema-hash",
        implementation_key="test-tool",
    )
    session = AsyncSession(engine, expire_on_commit=False)
    seeded: SeededWorkspace | None = None
    try:
        session.add_all([owner, operator, non_member, ws, owner_membership])
        await session.flush()
        session.add_all([agent, agent_version, connection])
        await session.flush()
        agent_binding = WorkspaceAgentBinding(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            agent_version_id=agent_version.id,
            enabled=True,
        )
        session.add(agent_binding)
        await session.flush()
        session.add_all([model_config_version, tool_definition, tool_version])
        await session.commit()
        seeded = SeededWorkspace(
            workspace=ws,
            owner=owner,
            operator=operator,
            non_member=non_member,
            owner_membership=owner_membership,
            agent=agent,
            agent_version=agent_version,
            agent_binding=agent_binding,
            model_config_version=model_config_version,
            tool_version=tool_version,
        )
        yield seeded
    finally:
        await session.rollback()
        if seeded is not None:
            await _cleanup(session, ws.id, seeded)
        await session.close()


def _bearer(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


async def _make_client(
    engine: AsyncEngine, headers: dict[str, str]
) -> AsyncIterator[AsyncClient]:
    app = create_app(engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=headers
    ) as client:
        yield client


@pytest.fixture()
async def owner_client(
    engine: AsyncEngine, workspace: SeededWorkspace
) -> AsyncIterator[AsyncClient]:
    async for client in _make_client(engine, _bearer(workspace.owner.id)):
        yield client


@pytest.fixture()
async def operator_client(
    engine: AsyncEngine, workspace: SeededWorkspace
) -> AsyncIterator[AsyncClient]:
    async for client in _make_client(engine, _bearer(workspace.operator.id)):
        yield client


@pytest.fixture()
async def non_member_client(
    engine: AsyncEngine, workspace: SeededWorkspace
) -> AsyncIterator[AsyncClient]:
    async for client in _make_client(engine, _bearer(workspace.non_member.id)):
        yield client


@pytest.fixture()
async def session_client(
    owner_client: AsyncClient, workspace: SeededWorkspace
) -> AsyncClient:
    """计划原文测试引用的客户端:owner 身份,已可用种子能力创建 Session。"""
    return owner_client


async def _create_session(client: AsyncClient, workspace_id: UUID) -> dict[str, object]:
    response = await client.post(
        "/api/sessions",
        json={"workspace_id": str(workspace_id), "title": "t"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def _submit_message(
    client: AsyncClient, session_id: UUID, content: str = "do the task"
) -> dict[str, object]:
    response = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": content},
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def _create_run(
    owner_client: AsyncClient, workspace: SeededWorkspace
) -> CreatedRun:
    """经 API 创建 Session 并提交首条消息,返回消息与 Run 标识。"""
    created = await _create_session(owner_client, workspace.id)
    session_id = UUID(str(created["id"]))
    dispatched = await _submit_message(owner_client, session_id)
    return CreatedRun(
        session_id=session_id,
        message_id=UUID(str(dispatched["message_id"])),
        run_id=UUID(str(dispatched["run_id"])),
    )


async def _transition(
    engine: AsyncEngine, workspace_id: UUID, run_id: UUID, *targets: RunStatus
) -> None:
    """按顺序把 Run 推过给定状态(测试内直接驱动 RunService,不走 Worker)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            service = RunService(session=session, workspace_id=workspace_id)
            for target in targets:
                await service.transition_run(
                    run_id=run_id,
                    target_status=target,
                    event_kind=f"TEST_{target.value}",
                    event_payload={},
                )
    finally:
        await session.close()


async def _run_row(engine: AsyncEngine, workspace_id: UUID, run_id: UUID) -> Run:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        row = (
            await session.scalars(
                select(Run).where(Run.workspace_id == workspace_id, Run.id == run_id)
            )
        ).one()
        return row
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# 计划原文测试(Task 3 Step 1)
# ---------------------------------------------------------------------------


async def test_waiting_run_blocks_new_user_message(
    session_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created = await _create_run(session_client, workspace)
    await _transition(
        engine, workspace.id, created.run_id, RunStatus.RUNNING, RunStatus.WAITING_INPUT
    )
    response = await session_client.post(
        f"/api/sessions/{created.session_id}/messages",
        json={"content": "start another task"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ACTIVE_RUN_EXISTS"


# ---------------------------------------------------------------------------
# 创建 Session 与提交消息
# ---------------------------------------------------------------------------


def test_create_session_request_forbids_client_selected_agent() -> None:
    request = CreateSessionRequest(workspace_id=uuid.uuid4())

    assert request.title is None
    with pytest.raises(ValidationError):
        CreateSessionRequest(
            workspace_id=uuid.uuid4(),
            agent_id=uuid.uuid4(),
        )


async def test_create_session_binds_current_actor(
    owner_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    response = await owner_client.post(
        "/api/sessions",
        json={"workspace_id": str(workspace.id)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace_id"] == str(workspace.id)
    assert body["agent_id"] == str(workspace.agent_id)
    assert body["agent_version_id"] == str(workspace.agent_version.id)
    assert body["created_by_actor_id"] == str(workspace.owner.id)
    assert body["status"] == "ACTIVE"


@pytest.mark.parametrize("binding_state", ["disabled", "future"])
async def test_create_session_rejects_inactive_workspace_agent_binding(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
    binding_state: str,
) -> None:
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        binding = await db.get(WorkspaceAgentBinding, workspace.agent_binding.id)
        assert binding is not None
        if binding_state == "disabled":
            binding.enabled = False
        else:
            binding.effective_at = datetime.now(UTC) + timedelta(days=1)
        await db.commit()
    finally:
        await db.close()

    response = await owner_client.post(
        "/api/sessions",
        json={"workspace_id": str(workspace.id)},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "AGENT_NOT_ENABLED_FOR_WORKSPACE"


async def test_create_session_rejects_multiple_enabled_agent_versions(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    v2 = AgentVersion(
        id=uuid.uuid4(),
        agent_id=workspace.agent_id,
        version=2,
        status=ConfigVersionStatus.PUBLISHED,
        definition_json={"role": "second version"},
        definition_hash="second-version-hash",
    )
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        db.add(v2)
        await db.flush()
        db.add(
            WorkspaceAgentBinding(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                agent_version_id=v2.id,
                enabled=True,
            )
        )
        await db.commit()
    finally:
        await db.close()

    response = await owner_client.post(
        "/api/sessions",
        json={"workspace_id": str(workspace.id)},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "WORKSPACE_AGENT_CONFIGURATION_INVALID"


async def test_agent_binding_in_another_workspace_does_not_authorize_session(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    other_workspace = Workspace(id=uuid.uuid4(), name="other workspace")
    other_binding = WorkspaceAgentBinding(
        id=uuid.uuid4(),
        workspace_id=other_workspace.id,
        agent_version_id=workspace.agent_version.id,
        enabled=True,
    )
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        binding = await db.get(WorkspaceAgentBinding, workspace.agent_binding.id)
        assert binding is not None
        binding.enabled = False
        db.add(other_workspace)
        await db.flush()
        db.add(other_binding)
        await db.commit()
    finally:
        await db.close()

    try:
        response = await owner_client.post(
            "/api/sessions",
            json={"workspace_id": str(workspace.id)},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "AGENT_NOT_ENABLED_FOR_WORKSPACE"
    finally:
        db = AsyncSession(engine, expire_on_commit=False)
        try:
            await db.execute(
                sa.delete(WorkspaceAgentBinding).where(
                    WorkspaceAgentBinding.id == other_binding.id
                )
            )
            await db.execute(sa.delete(Workspace).where(Workspace.id == other_workspace.id))
            await db.commit()
        finally:
            await db.close()


async def test_non_member_cannot_create_session_in_workspace(
    non_member_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    response = await non_member_client.post(
        "/api/sessions",
        json={"workspace_id": str(workspace.id)},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_submit_message_creates_run_and_advance_job(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created_session = await _create_session(owner_client, workspace.id)
    session_id = UUID(str(created_session["id"]))

    # Publish and bind v2 after Session creation. Existing Session must remain pinned to v1.
    v2 = AgentVersion(
        id=uuid.uuid4(),
        agent_id=workspace.agent_id,
        version=2,
        status=ConfigVersionStatus.PUBLISHED,
        definition_json={"role": "updated test"},
        definition_hash="test-definition-hash-v2",
    )
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        binding = await db.get(WorkspaceAgentBinding, workspace.agent_binding.id)
        assert binding is not None
        binding.enabled = False
        db.add(v2)
        await db.flush()
        db.add(
            WorkspaceAgentBinding(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                agent_version_id=v2.id,
                enabled=True,
            )
        )
        await db.commit()
    finally:
        await db.close()

    dispatched = await _submit_message(owner_client, session_id, "price check")
    run_id = UUID(str(dispatched["run_id"]))
    message_id = UUID(str(dispatched["message_id"]))
    assert dispatched["status"] == "QUEUED"

    db = AsyncSession(engine, expire_on_commit=False)
    try:
        run = (
            await db.scalars(
                select(Run).where(Run.workspace_id == workspace.id, Run.id == run_id)
            )
        ).one()
        assert run.status.value == "QUEUED"
        assert run.session_id == session_id
        assert run.agent_version_id == workspace.agent_version.id
        assert run.trigger_message_id == message_id
        message = (
            await db.scalars(
                select(Message).where(
                    Message.workspace_id == workspace.id, Message.id == message_id
                )
            )
        ).one()
        assert message.role.value == "USER"
        assert message.status.value == "COMPLETED"
        assert message.content == "price check"
        assert message.run_id == run_id
        session_row = await db.get(Session, session_id)
        assert session_row is not None
        assert session_row.agent_version_id == workspace.agent_version.id
        # 命令层只落 QUEUED;推进由进程内执行器负责,此处不应产生 Attempt。
        attempts = (
            await db.scalars(
                select(RunAttempt).where(
                    RunAttempt.workspace_id == workspace.id,
                    RunAttempt.run_id == run_id,
                )
            )
        ).all()
        assert attempts == []
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# 私有 Session 与跨工作区
# ---------------------------------------------------------------------------


async def test_session_is_private_to_creator(
    owner_client: AsyncClient,
    operator_client: AsyncClient,
    non_member_client: AsyncClient,
    workspace: SeededWorkspace,
) -> None:
    created = await _create_run(owner_client, workspace)

    forbidden = await operator_client.get(f"/api/sessions/{created.session_id}")
    assert forbidden.status_code == 404

    outsider = await non_member_client.get(f"/api/sessions/{created.session_id}")
    assert outsider.status_code == 404

    snapshot = await owner_client.get(f"/api/sessions/{created.session_id}")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["id"] == str(created.session_id)
    assert [message["content"] for message in body["messages"]] == ["do the task"]
    assert [run["id"] for run in body["runs"]] == [str(created.run_id)]


async def test_resource_from_other_workspace_returns_404(
    owner_client: AsyncClient,
    engine: AsyncEngine,
    workspace: SeededWorkspace,
) -> None:
    """另一个工作区的 Session/Run 对本工作区成员一律 404。"""
    other_owner = User(
        id=uuid.uuid4(),
        login=f"other-owner-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    other_ws = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:8]}")
    other_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=other_ws.id,
        user_id=other_owner.id,
        role=MembershipRole.WORKSPACE_OWNER,
        status=AccountStatus.ACTIVE,
    )
    other_connection = ModelConnection(
        id=uuid.uuid4(),
        workspace_id=other_ws.id,
        stable_key=f"conn-{uuid.uuid4().hex[:12]}",
        name="other model",
    )
    other_model_config_version = ModelConfigVersion(
        id=uuid.uuid4(),
        workspace_id=other_ws.id,
        model_connection_id=other_connection.id,
        version=1,
        backend_type=ModelBackendType.OPENAI,
        base_url="https://model.invalid/v1",
        model="test-model",
        non_secret_options={},
        config_hash="test-config-hash",
    )
    db = AsyncSession(engine, expire_on_commit=False)
    try:
        db.add_all([other_owner, other_ws, other_membership])
        await db.flush()
        db.add(other_connection)
        await db.flush()
        db.add_all(
            [
                other_model_config_version,
                WorkspaceAgentBinding(
                    id=uuid.uuid4(),
                    workspace_id=other_ws.id,
                    agent_version_id=workspace.agent_version.id,
                    enabled=True,
                ),
            ]
        )
        await db.commit()
    finally:
        await db.close()

    app = create_app(engine=engine)
    headers = _bearer(other_owner.id)
    other_session: dict[str, object] | None = None
    other_run: dict[str, object] | None = None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=headers
    ) as other_client:
        try:
            other_session = await _create_session(other_client, other_ws.id)
            other_run = await _submit_message(
                other_client, UUID(str(other_session["id"]))
            )
            assert (
                await owner_client.get(f"/api/sessions/{other_session['id']}")
            ).status_code == 404
            assert (
                await owner_client.get(f"/api/runs/{other_run['run_id']}")
            ).status_code == 404
            assert (
                await owner_client.post(f"/api/runs/{other_run['run_id']}/cancel")
            ).status_code == 404
        finally:
            db = AsyncSession(engine, expire_on_commit=False)
            try:
                await db.execute(
                    sa.delete(AuditEvent).where(AuditEvent.workspace_id == other_ws.id)
                )
                await db.execute(
                    sa.delete(AgentEvent).where(AgentEvent.workspace_id == other_ws.id)
                )
                await db.execute(
                    sa.update(Message)
                    .where(Message.workspace_id == other_ws.id)
                    .values(run_id=None)
                )
                await db.execute(
                    sa.update(Run)
                    .where(Run.workspace_id == other_ws.id)
                    .values(trigger_message_id=None)
                )
                await db.execute(sa.delete(Run).where(Run.workspace_id == other_ws.id))
                await db.execute(
                    sa.delete(Message).where(Message.workspace_id == other_ws.id)
                )
                await db.execute(
                    sa.delete(Session).where(Session.workspace_id == other_ws.id)
                )
                await db.execute(
                    sa.delete(ModelConfigVersion).where(
                        ModelConfigVersion.workspace_id == other_ws.id
                    )
                )
                await db.execute(
                    sa.delete(ModelConnection).where(
                        ModelConnection.workspace_id == other_ws.id
                    )
                )
                await db.execute(
                    sa.delete(WorkspaceAgentBinding).where(
                        WorkspaceAgentBinding.workspace_id == other_ws.id
                    )
                )
                await db.execute(
                    sa.delete(WorkspaceMembership).where(
                        WorkspaceMembership.id == other_membership.id
                    )
                )
                await db.execute(sa.delete(Workspace).where(Workspace.id == other_ws.id))
                await db.execute(sa.delete(User).where(User.id == other_owner.id))
                await db.commit()
            finally:
                await db.close()


# ---------------------------------------------------------------------------
# 取消 Run
# ---------------------------------------------------------------------------


async def test_cancel_run_moves_to_cancelled(
    owner_client: AsyncClient,
    operator_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created = await _create_run(owner_client, workspace)

    forbidden = await operator_client.post(f"/api/runs/{created.run_id}/cancel")
    assert forbidden.status_code == 404

    response = await owner_client.post(f"/api/runs/{created.run_id}/cancel")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CANCELLED"

    run = await _run_row(engine, workspace.id, created.run_id)
    assert run.status.value == "CANCELLED"

    replay = await owner_client.post(f"/api/runs/{created.run_id}/cancel")
    assert replay.status_code == 409


# ---------------------------------------------------------------------------
# Input Request:单次回答
# ---------------------------------------------------------------------------


async def _make_pending_input(
    engine: AsyncEngine,
    workspace: SeededWorkspace,
    created: CreatedRun,
    field_paths: list[str],
) -> UUID:
    """在 WAITING_INPUT 的 Run 上创建 PENDING Input Request。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            request = await InputService(
                session, workspace_id=workspace.id, actor_id=workspace.owner.id
            ).create(
                session_id=created.session_id,
                run_id=created.run_id,
                field_paths=field_paths,
                question="need a value",
                response_schema={"type": "object"},
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            return request.id
    finally:
        await session.close()


async def test_answer_input_request_single_shot(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created = await _create_run(owner_client, workspace)
    await _transition(
        engine, workspace.id, created.run_id, RunStatus.RUNNING, RunStatus.WAITING_INPUT
    )
    request_id = await _make_pending_input(
        engine, workspace, created, field_paths=["target_price"]
    )

    undeclared = await owner_client.post(
        f"/api/requests/{request_id}/answer",
        json={"values": {"undeclared_field": "x"}},
    )
    assert undeclared.status_code == 422
    assert undeclared.json()["code"] == "UNDECLARED_FIELD"

    answered = await owner_client.post(
        f"/api/requests/{request_id}/answer",
        json={"values": {"target_price": "12.5"}},
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["status"] == "ANSWERED"
    assert body["run_status"] == "QUEUED"

    run = await _run_row(engine, workspace.id, created.run_id)
    assert run.status.value == "QUEUED"

    replay = await owner_client.post(
        f"/api/requests/{request_id}/answer",
        json={"values": {"target_price": "13.5"}},
    )
    assert replay.status_code == 409
    assert replay.json()["code"] == "REQUEST_NOT_PENDING"


# ---------------------------------------------------------------------------
# Confirmation Request:单次 accept / reject
# ---------------------------------------------------------------------------


async def _make_pending_confirmation(
    engine: AsyncEngine, workspace: SeededWorkspace, created: CreatedRun
) -> UUID:
    """为 Run 建一个带 ToolCall 的 PENDING Confirmation Request。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            message = (
                await session.scalars(
                    select(Message).where(Message.id == created.message_id)
                )
            ).one()
            tool_call = ToolCall(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                run_id=created.run_id,
                message_id=message.id,
                tool_version_id=workspace.tool_version.id,
                ordinal=1,
                raw_arguments={"target_price": "12.5"},
            )
            session.add(tool_call)
            await session.flush()
            request = await ConfirmationService(
                session, workspace_id=workspace.id, actor_id=workspace.owner.id
            ).create(
                session_id=created.session_id,
                run_id=created.run_id,
                tool_call_id=tool_call.id,
                normalized_arguments={"target_price": "12.5"},
                resource_versions={"listing:store-1/sku-1": 1},
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
            return request.id
    finally:
        await session.close()


async def test_confirmation_accept_single_shot(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created = await _create_run(owner_client, workspace)
    await _transition(
        engine,
        workspace.id,
        created.run_id,
        RunStatus.RUNNING,
        RunStatus.WAITING_CONFIRMATION,
    )
    request_id = await _make_pending_confirmation(engine, workspace, created)

    accepted = await owner_client.post(
        f"/api/requests/{request_id}/confirmation/accept"
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["status"] == "ACCEPTED"
    assert body["run_status"] == "QUEUED"

    run = await _run_row(engine, workspace.id, created.run_id)
    assert run.status.value == "QUEUED"

    replay = await owner_client.post(
        f"/api/requests/{request_id}/confirmation/accept"
    )
    assert replay.status_code == 409
    assert replay.json()["code"] == "REQUEST_NOT_PENDING"


async def test_confirmation_reject_single_shot(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created = await _create_run(owner_client, workspace)
    await _transition(
        engine,
        workspace.id,
        created.run_id,
        RunStatus.RUNNING,
        RunStatus.WAITING_CONFIRMATION,
    )
    request_id = await _make_pending_confirmation(engine, workspace, created)

    rejected = await owner_client.post(
        f"/api/requests/{request_id}/confirmation/reject"
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "REJECTED"

    replay = await owner_client.post(
        f"/api/requests/{request_id}/confirmation/reject"
    )
    assert replay.status_code == 409
    assert replay.json()["code"] == "REQUEST_NOT_PENDING"


# ---------------------------------------------------------------------------
# Run 快照
# ---------------------------------------------------------------------------


async def test_run_snapshot_includes_messages_tool_calls_and_cursor(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    created = await _create_run(owner_client, workspace)
    await owner_client.post(f"/api/runs/{created.run_id}/cancel")

    response = await owner_client.get(f"/api/runs/{created.run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(created.run_id)
    assert body["session_id"] == str(created.session_id)
    assert body["status"] == "CANCELLED"
    assert [message["id"] for message in body["messages"]] == [str(created.message_id)]
    assert body["tool_calls"] == []
    # 取消写入了恰好一条 AgentEvent(RUN_CANCELLED),游标指向最后已持久序号。
    assert body["events_cursor"] == 1
