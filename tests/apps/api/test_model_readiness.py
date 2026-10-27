"""Workspace 执行前置条件:模型配置与价格策略。

覆盖三件事:

1. 没配模型连接时,readiness 明确报 ``MODEL_NOT_CONFIGURED``;
2. 配了连接但只写了占位行(无 ``secret_id``)时同样不可用——占位行拿不到 Key;
3. 真实配置(密文 Key + 已发布价格策略)能被解析出明文,readiness 通过。

这里不启动执行器、不调用模型:验证的是"配置是否可解析",不是推理质量。
"""

import base64
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.policy.models import PricePolicyConfig
from agents.product_operations.pricing.configuration import (
    PRICING_POLICY_STABLE_KEY,
    canonical_pricing_policy_payload,
    pricing_policy_config_hash,
)
from apps.api.main import create_app
from apps.model_driver import (
    MODEL_NOT_CONFIGURED,
    PRICING_POLICY_NOT_CONFIGURED,
    WorkspaceNotReady,
    check_workspace_readiness,
    load_published_pricing_config,
    load_workspace_model_config,
)
from runtime.access.tokens import issue_token
from runtime.persistence.base import (
    AccountStatus,
    ConfigVersionStatus,
    MembershipRole,
    ModelBackendType,
)
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    BusinessPolicy,
    BusinessPolicyVersion,
    ModelConfigVersion,
    ModelConnection,
    WorkspaceAgentBinding,
)
from runtime.persistence.models.execution import Run
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.integrations import Secret
from runtime.persistence.models.interaction import Session
from runtime.security.secrets import SecretCipher, build_secret_binding


PLAIN_KEY = "sk-test-refactor-key"
BASE_URL = "https://model.example.com/v1"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """给出合法主密钥;密钥材料固定,不产生随机性。"""
    monkeypatch.setenv("RUNTIME_MASTER_KEY", base64.b64encode(b"k" * 32).decode())


async def _seed_workspace(session: AsyncSession) -> tuple[Workspace, User]:
    user = User(
        id=uuid.uuid4(),
        login=f"model-{uuid.uuid4().hex[:12]}",
        password_hash="not-a-real-hash",
        status=AccountStatus.ACTIVE,
    )
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:8]}")
    session.add_all([user, workspace])
    await session.flush()
    return workspace, user


async def _add_model_version(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    with_secret: bool,
) -> UUID:
    """写入一条模型配置版本;``with_secret`` 控制是否附加密的 API Key。"""
    connection = ModelConnection(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        stable_key=f"conn-{uuid.uuid4().hex[:8]}",
        name="workspace model",
    )
    session.add(connection)
    await session.flush()
    secret_id: UUID | None = None
    if with_secret:
        cipher = SecretCipher()
        binding = build_secret_binding(
            workspace_id=str(workspace_id),
            connection_type="MODEL_API_KEY",
            normalized_endpoint=BASE_URL,
        )
        encrypted = cipher.encrypt(PLAIN_KEY.encode("utf-8"), binding=binding)
        secret = Secret(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            kind="MODEL_API_KEY",
            endpoint_binding=BASE_URL,
            key_version=encrypted.key_version,
            nonce=encrypted.nonce,
            ciphertext=encrypted.ciphertext,
            auth_tag=encrypted.tag,
            created_by_actor_id=user_id,
        )
        session.add(secret)
        await session.flush()
        secret_id = secret.id
    version = ModelConfigVersion(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        model_connection_id=connection.id,
        version=1,
        backend_type=ModelBackendType.OPENAI,
        base_url=BASE_URL,
        model="deepseek-chat",
        non_secret_options={},
        secret_id=secret_id,
        config_hash=f"hash-{uuid.uuid4().hex[:8]}",
    )
    session.add(version)
    await session.flush()
    return version.id


async def _add_published_policy(session: AsyncSession, *, workspace_id: UUID) -> None:
    config = PricePolicyConfig(
        policy_version="price-policy-v1",
        currency="CNY",
        min_allowed_price=Decimal("50.00"),
        max_price_change=Decimal("100.00"),
    )
    payload = canonical_pricing_policy_payload(config)
    policy = BusinessPolicy(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        stable_key=PRICING_POLICY_STABLE_KEY,
        name="Product pricing policy",
    )
    session.add(policy)
    await session.flush()
    session.add(
        BusinessPolicyVersion(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            business_policy_id=policy.id,
            version=1,
            config_json=payload,
            config_hash=pricing_policy_config_hash(payload),
            status=ConfigVersionStatus.PUBLISHED,
            published_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _purge(session: AsyncSession, workspace_id: UUID, user_id: UUID) -> None:
    """按外键顺序清理本文件写入的行。"""
    await session.execute(
        sa.delete(BusinessPolicyVersion).where(
            BusinessPolicyVersion.workspace_id == workspace_id
        )
    )
    await session.execute(
        sa.delete(BusinessPolicy).where(BusinessPolicy.workspace_id == workspace_id)
    )
    await session.execute(
        sa.delete(ModelConfigVersion).where(ModelConfigVersion.workspace_id == workspace_id)
    )
    await session.execute(
        sa.delete(ModelConnection).where(ModelConnection.workspace_id == workspace_id)
    )
    await session.execute(sa.delete(Secret).where(Secret.workspace_id == workspace_id))
    await session.execute(sa.delete(Workspace).where(Workspace.id == workspace_id))
    await session.execute(sa.delete(User).where(User.id == user_id))
    await session.commit()


@pytest.fixture()
async def scoped_workspace(engine: AsyncEngine) -> AsyncIterator[tuple[Workspace, User]]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        workspace, user = await _seed_workspace(session)
        await session.commit()
        yield workspace, user
    finally:
        await _purge(session, workspace.id, user.id)
        await session.close()


async def test_workspace_without_model_connection_is_not_ready(
    engine: AsyncEngine, scoped_workspace: tuple[Workspace, User]
) -> None:
    """一条模型连接都没有:readiness 报未配置模型,驱动解析抛同名错误码。"""
    workspace, _ = scoped_workspace
    readiness = await check_workspace_readiness(engine, workspace.id)
    assert readiness.model is False
    assert readiness.pricing_policy is False
    assert readiness.failure is not None
    assert readiness.failure.code == MODEL_NOT_CONFIGURED

    with pytest.raises(WorkspaceNotReady) as error:
        await load_workspace_model_config(engine, workspace.id)
    assert error.value.code == MODEL_NOT_CONFIGURED
    assert error.value.retryable is False


async def test_placeholder_version_without_secret_is_not_usable(
    engine: AsyncEngine, scoped_workspace: tuple[Workspace, User]
) -> None:
    """有配置行但没有 API Key(provisioning 占位行)不算可用配置。"""
    workspace, user = scoped_workspace
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        await _add_model_version(
            session, workspace_id=workspace.id, user_id=user.id, with_secret=False
        )
        await session.commit()
    finally:
        await session.close()

    readiness = await check_workspace_readiness(engine, workspace.id)
    assert readiness.model is False
    with pytest.raises(WorkspaceNotReady) as error:
        await load_workspace_model_config(engine, workspace.id)
    assert "API Key" in error.value.message


async def test_configured_model_and_policy_are_ready(
    engine: AsyncEngine, scoped_workspace: tuple[Workspace, User]
) -> None:
    """密文 Key 能解回明文,已发布价格策略能被读到:两项条件同时成立。"""
    workspace, user = scoped_workspace
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        await _add_model_version(
            session, workspace_id=workspace.id, user_id=user.id, with_secret=True
        )
        await _add_published_policy(session, workspace_id=workspace.id)
        await session.commit()
    finally:
        await session.close()

    config = await load_workspace_model_config(engine, workspace.id)
    assert config.api_key == PLAIN_KEY
    assert config.model == "deepseek-chat"
    assert config.backend_type is ModelBackendType.OPENAI

    policy = await load_published_pricing_config(engine, workspace.id)
    assert policy.currency == "CNY"

    readiness = await check_workspace_readiness(engine, workspace.id)
    assert readiness.ready is True
    assert readiness.failure is None


async def test_missing_policy_reports_policy_code(
    engine: AsyncEngine, scoped_workspace: tuple[Workspace, User]
) -> None:
    """模型配好了但价格策略没发布:错误码指向策略,而不是含糊的"未就绪"。"""
    workspace, user = scoped_workspace
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        await _add_model_version(
            session, workspace_id=workspace.id, user_id=user.id, with_secret=True
        )
        await session.commit()
    finally:
        await session.close()

    readiness = await check_workspace_readiness(engine, workspace.id)
    assert readiness.model is True
    assert readiness.pricing_policy is False
    assert readiness.failure is not None
    assert readiness.failure.code == PRICING_POLICY_NOT_CONFIGURED


def test_master_key_is_only_a_test_value() -> None:
    """测试用的主密钥与生产变量同名,确保 fixture 生效而不是偶然没配。"""
    assert os.environ.get("RUNTIME_MASTER_KEY")


# ---------------------------------------------------------------------------
# 真实入口:进 lifespan、装配生产 driver,提交消息被明确拒绝
# ---------------------------------------------------------------------------


async def _purge_full(session: AsyncSession, workspace_id: UUID, user_id: UUID) -> None:
    """清理带会话/绑定/Agent 的完整种子。"""
    await session.execute(sa.delete(Run).where(Run.workspace_id == workspace_id))
    await session.execute(sa.delete(Session).where(Session.workspace_id == workspace_id))
    agent_version_ids = list(
        await session.scalars(
            sa.select(WorkspaceAgentBinding.agent_version_id).where(
                WorkspaceAgentBinding.workspace_id == workspace_id
            )
        )
    )
    await session.execute(
        sa.delete(WorkspaceAgentBinding).where(
            WorkspaceAgentBinding.workspace_id == workspace_id
        )
    )
    if agent_version_ids:
        agent_ids = list(
            await session.scalars(
                sa.select(AgentVersion.agent_id).where(AgentVersion.id.in_(agent_version_ids))
            )
        )
        await session.execute(sa.delete(AgentVersion).where(AgentVersion.id.in_(agent_version_ids)))
        if agent_ids:
            await session.execute(sa.delete(Agent).where(Agent.id.in_(agent_ids)))
    await session.execute(
        sa.delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id)
    )
    await _purge(session, workspace_id, user_id)


@pytest.fixture()
async def real_entry_client(engine: AsyncEngine) -> AsyncIterator[tuple[AsyncClient, UUID]]:
    """生产入口 + 完整 lifespan;Workspace 没有任何模型配置。

    只有真正进入 lifespan 才会装配生产 driver,``requires_model_config`` 校验
    才生效——这里刻意走真实启动路径,而不是绕过。
    """
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        workspace, user = await _seed_workspace(session)
        session.add(
            WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.WORKSPACE_OWNER,
                status=AccountStatus.ACTIVE,
            )
        )
        agent = Agent(
            id=uuid.uuid4(),
            stable_key=f"agent-{workspace.id}",
            name="test agent",
        )
        agent_version = AgentVersion(
            id=uuid.uuid4(),
            agent_id=agent.id,
            version=1,
            status=ConfigVersionStatus.PUBLISHED,
            definition_json={"role": "test"},
            definition_hash="test-definition-hash",
        )
        session.add_all([agent, agent_version])
        await session.flush()
        session.add(
            WorkspaceAgentBinding(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                agent_version_id=agent_version.id,
                enabled=True,
            )
        )
        await session.commit()
    finally:
        await session.close()

    app = create_app(engine=engine)
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=headers
    ) as client:
        yield client, workspace.id

    session = AsyncSession(engine, expire_on_commit=False)
    try:
        await _purge_full(session, workspace.id, user.id)
    finally:
        await session.close()


async def test_message_is_rejected_when_workspace_has_no_model_config(
    real_entry_client: tuple[AsyncClient, UUID], engine: AsyncEngine
) -> None:
    """没配模型的 Workspace 提交消息:409 + 稳定错误码,且不留下 Run。"""
    client, workspace_id = real_entry_client
    created = await client.post("/api/sessions", json={"workspace_id": str(workspace_id)})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    response = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": "把 SKU-001 改成 99 元"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == MODEL_NOT_CONFIGURED
    assert "模型" in body["message"]
    assert body["request_id"]

    session = AsyncSession(engine, expire_on_commit=False)
    try:
        run_count = await session.scalar(
            sa.select(sa.func.count()).select_from(Run).where(Run.workspace_id == workspace_id)
        )
    finally:
        await session.close()
    assert run_count == 0

