"""Workspace 配置 API 权限测试(计划 04 Task 2;架构文档 §3、§4、§8、§10)。

夹具沿用 test_auth 的模式:种子显式提交、测试结束按外键顺序清理;
owner/operator 客户端分别持有 WORKSPACE_OWNER 与 OPERATOR 成员的有效 JWT。
覆盖:Owner 可配置、Operator 只读、最后 Owner 保护、权限/资源范围授予与收回、
Secret 掩码与密文落库、配置变更审计、非成员不泄漏工作区存在性。
"""

import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from apps.api.main import create_app
from runtime.access.actor import ActorContextBuilder
from runtime.access.authorization import Authorizer
from runtime.access.tokens import issue_token
from runtime.persistence.base import AccountStatus, MembershipRole
from runtime.persistence.models.configuration import (
    BusinessPolicy,
    BusinessPolicyVersion,
    ModelConfigVersion,
    ModelConnection,
    Skill,
    SkillVersion,
    Source,
    SourceVersion,
)
from runtime.persistence.models.events import AuditEvent
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
    User,
    Workspace,
    WorkspaceMembership,
)
from runtime.persistence.models.integrations import Secret


# bytes(range(32)):确定性占位主密钥,仅用于测试,不是真实密钥。
_TEST_MASTER_KEY_B64 = base64.b64encode(bytes(range(32))).decode("ascii")

API_KEY = "sk-test-1234567890abcdef"
NEW_API_KEY = "sk-rotated-0987654321fedcba"
BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class SeededWorkspace:
    """两个成员的 Workspace 种子:唯一 Owner、一个 Operator 与一个非成员。"""

    workspace: Workspace
    owner: User
    operator: User
    non_member: User
    owner_membership: WorkspaceMembership
    operator_membership: WorkspaceMembership

    @property
    def id(self) -> UUID:
        return self.workspace.id

    @property
    def only_owner_id(self) -> UUID:
        """唯一的 WORKSPACE_OWNER 成员身份 ID(计划原文测试引用)。"""
        return self.owner_membership.id


async def _cleanup(session: AsyncSession, seeded: SeededWorkspace) -> None:
    """按外键顺序删除种子与其派生行;已删除的行重复删除无副作用。"""
    workspace_id = seeded.id
    targets: list[tuple[type, object]] = [
        (AuditEvent, AuditEvent.workspace_id == workspace_id),
        (ModelConfigVersion, ModelConfigVersion.workspace_id == workspace_id),
        (Secret, Secret.workspace_id == workspace_id),
        (ModelConnection, ModelConnection.workspace_id == workspace_id),
        (SourceVersion, SourceVersion.workspace_id == workspace_id),
        (Source, Source.workspace_id == workspace_id),
        (SkillVersion, SkillVersion.workspace_id == workspace_id),
        (Skill, Skill.workspace_id == workspace_id),
        (BusinessPolicyVersion, BusinessPolicyVersion.workspace_id == workspace_id),
        (BusinessPolicy, BusinessPolicy.workspace_id == workspace_id),
        (MembershipPermission, MembershipPermission.workspace_id == workspace_id),
        (MembershipResourceScope, MembershipResourceScope.workspace_id == workspace_id),
        (MembershipResourceScopeMode, MembershipResourceScopeMode.workspace_id == workspace_id),
        (WorkspaceMembership, WorkspaceMembership.workspace_id == workspace_id),
        (Workspace, Workspace.id == workspace_id),
        (
            User,
            User.id.in_([seeded.owner.id, seeded.operator.id, seeded.non_member.id]),
        ),
    ]
    for table, condition in targets:
        await session.execute(sa.delete(table).where(condition))  # type: ignore[arg-type]
    await session.commit()


@pytest.fixture()
async def workspace(engine: AsyncEngine) -> AsyncIterator[SeededWorkspace]:
    """创建双成员 Workspace 种子并提交;结束后按外键顺序清理。"""
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
    operator_membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        user_id=operator.id,
        role=MembershipRole.OPERATOR,
        status=AccountStatus.ACTIVE,
    )
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add_all(
            [owner, operator, non_member, ws, owner_membership, operator_membership]
        )
        await session.commit()
        seeded = SeededWorkspace(
            workspace=ws,
            owner=owner,
            operator=operator,
            non_member=non_member,
            owner_membership=owner_membership,
            operator_membership=operator_membership,
        )
        yield seeded
    finally:
        await session.rollback()
        await _cleanup(session, seeded)  # type: ignore[possibly-undefined]
        await session.close()


def _bearer(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token(user_id)}"}


async def _make_client(engine: AsyncEngine, headers: dict[str, str]) -> AsyncIterator[AsyncClient]:
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
def master_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("RUNTIME_MASTER_KEY", _TEST_MASTER_KEY_B64)
    return _TEST_MASTER_KEY_B64


# ---------------------------------------------------------------------------
# 计划原文测试(Task 2 Step 1)
# ---------------------------------------------------------------------------


async def test_operator_cannot_change_execution_mode(
    operator_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    response = await operator_client.patch(
        f"/api/workspaces/{workspace.id}/execution-mode",
        json={"mode": "AUTO_EXECUTE"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_workspace_cannot_remove_its_last_owner(
    owner_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    response = await owner_client.delete(
        f"/api/workspaces/{workspace.id}/memberships/{workspace.only_owner_id}"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LAST_OWNER_REQUIRED"


# ---------------------------------------------------------------------------
# 读取语义:成员可读
# ---------------------------------------------------------------------------


async def test_owner_can_read_workspace_detail_and_members(
    owner_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    detail = await owner_client.get(f"/api/workspaces/{workspace.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == str(workspace.id)
    assert body["name"] == workspace.workspace.name
    assert body["execution_mode"] == "READ_ONLY"

    members = await owner_client.get(f"/api/workspaces/{workspace.id}/memberships")
    assert members.status_code == 200
    roles = {item["role"] for item in members.json()["memberships"]}
    assert roles == {"WORKSPACE_OWNER", "OPERATOR"}


async def test_operator_can_read_but_not_write_configuration(
    operator_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    base = f"/api/workspaces/{workspace.id}"
    assert (await operator_client.get(base)).status_code == 200
    assert (await operator_client.get(f"{base}/memberships")).status_code == 200
    assert (await operator_client.get(f"{base}/model-connections")).status_code == 200
    assert (await operator_client.get(f"{base}/skills")).status_code == 200
    assert (await operator_client.get(f"{base}/sources")).status_code == 200
    assert (await operator_client.get(f"{base}/business-policies")).status_code == 200

    attempts: list[tuple[str, str, dict[str, object]]] = [
        ("PATCH", "/execution-mode", {"mode": "AUTO_EXECUTE"}),
        ("POST", "/memberships", {"user_id": str(uuid.uuid4()), "role": "OPERATOR"}),
        (
            "POST",
            "/model-connections",
            {
                "stable_key": "openai-main",
                "name": "OpenAI",
                "backend_type": "OPENAI",
                "base_url": BASE_URL,
                "model": "gpt-4o",
                "api_key": API_KEY,
            },
        ),
        ("POST", "/skills", {"stable_key": "s", "name": "S", "content": {}}),
        (
            "POST",
            "/sources",
            {"stable_key": "mcp-main", "kind": "MCP", "name": "MCP", "config": {}},
        ),
        (
            "POST",
            "/business-policies",
            {"stable_key": "p", "name": "P", "config": {}},
        ),
    ]
    for method, suffix, payload in attempts:
        response = await operator_client.request(
            method, f"{base}{suffix}", json=payload
        )
        assert response.status_code == 403, (method, suffix)
        assert response.json()["code"] == "PERMISSION_DENIED"


async def test_operator_cannot_manage_member_authorizations(
    operator_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    membership_path = (
        f"/api/workspaces/{workspace.id}/memberships/{workspace.operator_membership.id}"
    )
    grant = await operator_client.post(
        f"{membership_path}/permissions", json={"permission": "product.price.write"}
    )
    assert grant.status_code == 403
    assert grant.json()["code"] == "PERMISSION_DENIED"

    scope = await operator_client.put(
        f"{membership_path}/resource-scopes",
        json={"resource_type": "store", "mode": "SELECTED", "resource_ids": ["store-1"]},
    )
    assert scope.status_code == 403

    role = await operator_client.patch(
        membership_path, json={"role": "WORKSPACE_OWNER"}
    )
    assert role.status_code == 403

    remove = await operator_client.delete(membership_path)
    assert remove.status_code == 403


# ---------------------------------------------------------------------------
# 写入语义:Owner 可配置
# ---------------------------------------------------------------------------


async def test_owner_updates_execution_mode(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    response = await owner_client.patch(
        f"/api/workspaces/{workspace.id}/execution-mode",
        json={"mode": "AUTO_EXECUTE"},
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "AUTO_EXECUTE"

    detail = await owner_client.get(f"/api/workspaces/{workspace.id}")
    assert detail.json()["execution_mode"] == "AUTO_EXECUTE"

    # 同一应用服务事务内追加审计事件:action 与 actor 可查。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.workspace_id == workspace.id)
            )
        ).all()
    assert [event.action for event in events] == ["EXECUTION_MODE_UPDATED"]
    event = events[0]
    assert event.actor_id == workspace.owner.id
    assert event.redacted_payload.get("mode") == "AUTO_EXECUTE"


async def test_owner_grants_and_revokes_action_permission(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    membership_path = (
        f"/api/workspaces/{workspace.id}/memberships/{workspace.operator_membership.id}"
    )
    grant = await owner_client.post(
        f"{membership_path}/permissions", json={"permission": "product.price.write"}
    )
    assert grant.status_code == 200
    assert "product.price.write" in grant.json()["permissions"]

    # GET 验证授权状态变化。
    fetched = await owner_client.get(membership_path)
    assert fetched.status_code == 200
    assert fetched.json()["permissions"] == ["product.price.write"]

    # Authorize 行为变化:重建 ActorContext 后放行该动作权限。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        context = await ActorContextBuilder(session).build(
            actor_id=workspace.operator.id,
            workspace_id=workspace.id,
            request_id="test-grant",
        )
    decision = Authorizer().authorize(
        context,
        required_permissions=frozenset({"product.price.write"}),
        resources=frozenset(),
    )
    assert decision.allowed is True

    revoke = await owner_client.delete(
        f"{membership_path}/permissions/product.price.write"
    )
    assert revoke.status_code == 200
    assert revoke.json()["permissions"] == []

    async with AsyncSession(engine, expire_on_commit=False) as session:
        context = await ActorContextBuilder(session).build(
            actor_id=workspace.operator.id,
            workspace_id=workspace.id,
            request_id="test-revoke",
        )
    decision = Authorizer().authorize(
        context,
        required_permissions=frozenset({"product.price.write"}),
        resources=frozenset(),
    )
    assert decision.allowed is False
    assert decision.reason_code == "PERMISSION_DENIED"


async def test_owner_grants_and_revokes_resource_scope(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
) -> None:
    membership_path = (
        f"/api/workspaces/{workspace.id}/memberships/{workspace.operator_membership.id}"
    )
    grant = await owner_client.put(
        f"{membership_path}/resource-scopes",
        json={
            "resource_type": "store",
            "mode": "SELECTED",
            "resource_ids": ["store-1", "store-2"],
        },
    )
    assert grant.status_code == 200
    scopes = grant.json()["resource_scopes"]
    assert scopes["store"]["mode"] == "SELECTED"
    assert set(scopes["store"]["resource_ids"]) == {"store-1", "store-2"}

    # Authorize 行为变化:范围内资源放行,范围外拒绝(RESOURCE_SCOPE_DENIED)。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        context = await ActorContextBuilder(session).build(
            actor_id=workspace.operator.id,
            workspace_id=workspace.id,
            request_id="test-scope",
        )
    authorizer = Authorizer()
    allowed = authorizer.authorize(
        context,
        required_permissions=frozenset(),
        resources=frozenset({("store", "store-1")}),
    )
    denied = authorizer.authorize(
        context,
        required_permissions=frozenset(),
        resources=frozenset({("store", "store-3")}),
    )
    assert allowed.allowed is True
    assert denied.allowed is False
    assert denied.reason_code == "RESOURCE_SCOPE_DENIED"

    revoke = await owner_client.delete(f"{membership_path}/resource-scopes/store")
    assert revoke.status_code == 200
    assert revoke.json()["resource_scopes"] == {}


# ---------------------------------------------------------------------------
# 模型连接:Secret 掩码、密文落库与审计
# ---------------------------------------------------------------------------


async def test_owner_creates_and_updates_model_connection_with_masked_secret(
    owner_client: AsyncClient,
    workspace: SeededWorkspace,
    engine: AsyncEngine,
    master_key: str,
) -> None:
    base = f"/api/workspaces/{workspace.id}"
    create = await owner_client.post(
        f"{base}/model-connections",
        json={
            "stable_key": "openai-main",
            "name": "OpenAI",
            "backend_type": "OPENAI",
            "base_url": BASE_URL,
            "model": "gpt-4o",
            "api_key": API_KEY,
            "non_secret_options": {"timeout": 30},
        },
    )
    assert create.status_code == 201
    body = create.json()
    assert body["masked_key"] == "****" + API_KEY[-4:]
    assert body["version"] == 1
    assert body["updated_at"]
    assert API_KEY not in create.text
    connection_id = body["id"]

    update = await owner_client.patch(
        f"{base}/model-connections/{connection_id}",
        json={"api_key": NEW_API_KEY, "model": "gpt-4o-mini"},
    )
    assert update.status_code == 200
    update_body = update.json()
    assert update_body["masked_key"] == "****" + NEW_API_KEY[-4:]
    assert update_body["version"] == 2
    assert API_KEY not in update.text
    assert NEW_API_KEY not in update.text

    fetched = await owner_client.get(f"{base}/model-connections/{connection_id}")
    assert fetched.status_code == 200
    assert API_KEY not in fetched.text
    assert NEW_API_KEY not in fetched.text

    # 落库为 AES-GCM 密文:secrets 与 model_config_versions 行均不含明文。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        secret_rows = (
            await session.scalars(
                select(Secret).where(Secret.workspace_id == workspace.id)
            )
        ).all()
        version_rows = (
            await session.scalars(
                select(ModelConfigVersion).where(
                    ModelConfigVersion.workspace_id == workspace.id
                )
            )
        ).all()
    assert len(secret_rows) == 2
    assert len(version_rows) == 2
    for row in secret_rows:
        blob = row.nonce + row.ciphertext + (row.auth_tag or b"")
        assert API_KEY.encode() not in blob
        assert NEW_API_KEY.encode() not in blob
    for row in version_rows:
        assert API_KEY not in json.dumps(row.non_secret_options)

    # 两次配置变更各有一条审计事件,payload 不含明文。
    async with AsyncSession(engine, expire_on_commit=False) as session:
        events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.workspace_id == workspace.id)
            )
        ).all()
    assert {event.action for event in events} == {
        "MODEL_CONNECTION_CONFIGURED",
        "MODEL_CONNECTION_UPDATED",
    }
    for event in events:
        assert event.actor_id == workspace.owner.id
        assert API_KEY not in json.dumps(event.redacted_payload)
        assert NEW_API_KEY not in json.dumps(event.redacted_payload)


async def test_operator_cannot_update_model_connection(
    owner_client: AsyncClient,
    operator_client: AsyncClient,
    workspace: SeededWorkspace,
    master_key: str,
) -> None:
    create = await owner_client.post(
        f"/api/workspaces/{workspace.id}/model-connections",
        json={
            "stable_key": "openai-main",
            "name": "OpenAI",
            "backend_type": "OPENAI",
            "base_url": BASE_URL,
            "model": "gpt-4o",
            "api_key": API_KEY,
        },
    )
    assert create.status_code == 201
    connection_id = create.json()["id"]

    response = await operator_client.patch(
        f"/api/workspaces/{workspace.id}/model-connections/{connection_id}",
        json={"model": "gpt-4o-mini"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Skill / Source / Business Policy 端点
# ---------------------------------------------------------------------------


async def test_owner_manages_skill_source_and_business_policy(
    owner_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    base = f"/api/workspaces/{workspace.id}"
    skill = await owner_client.post(
        f"{base}/skills",
        json={"stable_key": "price-report", "name": "价格报告", "content": {"steps": 3}},
    )
    assert skill.status_code == 201
    assert skill.json()["latest_version"] == 1
    skills = await owner_client.get(f"{base}/skills")
    assert [item["stable_key"] for item in skills.json()["skills"]] == ["price-report"]

    source = await owner_client.post(
        f"{base}/sources",
        json={"stable_key": "mcp-main", "kind": "MCP", "name": "MCP Server", "config": {}},
    )
    assert source.status_code == 201
    sources = await owner_client.get(f"{base}/sources")
    assert [item["stable_key"] for item in sources.json()["sources"]] == ["mcp-main"]

    policy = await owner_client.post(
        f"{base}/business-policies",
        json={"stable_key": "price-guard", "name": "价格护栏", "config": {"max": 0.1}},
    )
    assert policy.status_code == 201
    policies = await owner_client.get(f"{base}/business-policies")
    assert [item["stable_key"] for item in policies.json()["business_policies"]] == [
        "price-guard"
    ]


# ---------------------------------------------------------------------------
# Workspace 隔离:非成员不泄漏存在性
# ---------------------------------------------------------------------------


async def test_non_member_cannot_access_workspace_or_learn_existence(
    non_member_client: AsyncClient, workspace: SeededWorkspace
) -> None:
    existing = await non_member_client.get(f"/api/workspaces/{workspace.id}")
    assert existing.status_code == 403
    assert existing.json()["code"] == "PERMISSION_DENIED"

    missing = await non_member_client.get(f"/api/workspaces/{uuid.uuid4()}")
    assert missing.status_code == 403
    assert missing.json()["code"] == "PERMISSION_DENIED"
