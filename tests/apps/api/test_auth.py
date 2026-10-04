"""API 认证与身份链路测试(计划 04 Task 1;架构文档 §2、§6)。

夹具:
- ``api_client``:httpx AsyncClient + ASGITransport,直接调用 ASGI 应用,不监听端口;
- ``seeded_user``:user + workspace + membership 种子(密码 argon2 哈希)。
  应用通过自身 Session(独立连接)读库,种子数据显式提交,测试结束清理,
  避免污染同会话内的其他测试。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from apps.api.main import create_app
from runtime.access.passwords import hash_password
from runtime.access.tokens import issue_token
from runtime.persistence.base import AccountStatus, MembershipRole
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership


PASSWORD = "correct-password"


@dataclass(frozen=True)
class SeededUser:
    """最小身份种子:一个用户、一个工作区及其成员身份。"""

    user: User
    workspace: Workspace
    membership: WorkspaceMembership


@pytest.fixture()
async def seeded_user(engine: AsyncEngine) -> AsyncIterator[SeededUser]:
    """创建 user + workspace + membership 种子并提交;结束后删除种子行。"""
    user = User(
        id=uuid.uuid4(),
        login=f"login-{uuid.uuid4().hex[:12]}",
        password_hash=hash_password(PASSWORD),
        status=AccountStatus.ACTIVE,
    )
    workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4().hex[:8]}")
    membership = WorkspaceMembership(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=user.id,
        role=MembershipRole.OPERATOR,
        status=AccountStatus.ACTIVE,
    )
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        session.add_all([user, workspace, membership])
        await session.commit()
        yield SeededUser(user=user, workspace=workspace, membership=membership)
    finally:
        await session.rollback()
        # 先删成员身份再删工作区与用户,满足外键顺序;已删除的行重复删除无副作用。
        await session.execute(
            sa.delete(WorkspaceMembership).where(WorkspaceMembership.id == membership.id)
        )
        await session.execute(sa.delete(Workspace).where(Workspace.id == workspace.id))
        await session.execute(sa.delete(User).where(User.id == user.id))
        await session.commit()
        await session.close()


@pytest.fixture()
async def api_client(engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """注入测试引擎的应用客户端;ASGITransport 不监听端口、不触发 lifespan。"""
    app = create_app(engine=engine)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def test_login_returns_short_lived_token(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": PASSWORD},
    )
    assert response.status_code == 200
    assert set(response.json()) == {"access_token", "token_type", "expires_in"}


async def test_actor_header_cannot_impersonate_user(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/workspaces", headers={"X-Actor-Id": "admin"})
    assert response.status_code == 401


async def test_workspaces_requires_authentication(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/workspaces")
    assert response.status_code == 401


async def test_wrong_password_returns_invalid_credentials(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"


async def test_unknown_login_same_error_as_wrong_password(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    unknown = await api_client.post(
        "/api/auth/login",
        json={"login": f"missing-{uuid.uuid4().hex[:12]}", "password": "whatever"},
    )
    wrong_password = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": "wrong-password"},
    )
    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["code"] == wrong_password.json()["code"] == "INVALID_CREDENTIALS"


async def test_expired_token_is_rejected(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    token = issue_token(seeded_user.user.id, expires_in=timedelta(seconds=-30))
    response = await api_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_INVALID"


async def test_token_signed_by_other_secret_is_rejected(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    token = issue_token(seeded_user.user.id, secret="attacker-secret-not-used-in-production")
    response = await api_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "TOKEN_INVALID"


async def test_valid_token_lists_current_workspaces(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    login_response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": PASSWORD},
    )
    token = login_response.json()["access_token"]
    response = await api_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    workspaces = response.json()["workspaces"]
    assert [uuid.UUID(item["id"]) for item in workspaces] == [seeded_user.workspace.id]
    assert workspaces[0]["name"] == seeded_user.workspace.name


async def test_removed_membership_rejects_old_token(
    api_client: AsyncClient, seeded_user: SeededUser, engine: AsyncEngine
) -> None:
    login_response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": PASSWORD},
    )
    token = login_response.json()["access_token"]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            sa.delete(WorkspaceMembership).where(
                WorkspaceMembership.id == seeded_user.membership.id
            )
        )
        await session.commit()

    response = await api_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_disabled_membership_rejects_old_token(
    api_client: AsyncClient, seeded_user: SeededUser, engine: AsyncEngine
) -> None:
    login_response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": PASSWORD},
    )
    token = login_response.json()["access_token"]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            sa.update(WorkspaceMembership)
            .where(WorkspaceMembership.id == seeded_user.membership.id)
            .values(status=AccountStatus.DISABLED)
        )
        await session.commit()

    response = await api_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


async def test_disabled_user_rejects_old_token(
    api_client: AsyncClient, seeded_user: SeededUser, engine: AsyncEngine
) -> None:
    login_response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": PASSWORD},
    )
    token = login_response.json()["access_token"]
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(
            sa.update(User)
            .where(User.id == seeded_user.user.id)
            .values(status=AccountStatus.DISABLED)
        )
        await session.commit()

    response = await api_client.get(
        "/api/workspaces", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "ACTOR_UNAVAILABLE"


async def test_error_body_is_uniform_and_request_id_is_transparent(
    api_client: AsyncClient, seeded_user: SeededUser
) -> None:
    response = await api_client.post(
        "/api/auth/login",
        json={"login": seeded_user.user.login, "password": "wrong-password"},
        headers={"X-Request-Id": "req-fixed-1"},
    )
    body = response.json()
    assert set(body) == {"code", "message", "request_id"}
    assert isinstance(body["message"], str)
    assert body["request_id"] == "req-fixed-1"
    assert response.headers["x-request-id"] == "req-fixed-1"


async def test_validation_error_uses_uniform_body(api_client: AsyncClient) -> None:
    response = await api_client.post("/api/auth/login", json={"password": "x"})
    assert response.status_code == 422
    assert set(response.json()) == {"code", "message", "request_id"}
    assert response.json()["code"] == "VALIDATION_ERROR"
