"""RBAC API 测试 — 创建会话绑定角色、默认角色、非法角色回退、列表展示 user。"""

import os
import pytest
from httpx import ASGITransport, AsyncClient
from transport.server import app, sessions, workspaces


@pytest.fixture(autouse=True)
def cleanup_sessions():
    sessions.clear()
    workspaces.clear()
    storage_dir = os.environ.get("AGENT_STORAGE_DIR")
    if storage_dir and os.path.isdir(storage_dir):
        for fname in os.listdir(storage_dir):
            if fname.endswith(".jsonl"):
                os.remove(os.path.join(storage_dir, fname))
    yield


@pytest.mark.asyncio
async def test_create_session_with_role():
    """带 body 创建客服会话，返回的会话身份应为 customer_service。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sessions", json={"user_id": "cs_01", "role": "customer_service"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["user_id"] == "cs_01"
        assert data["user"]["role"] == "customer_service"


@pytest.mark.asyncio
async def test_create_session_default_operator():
    """无 body 创建会话：默认运营角色。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sessions")
        data = resp.json()
        assert data["user"]["role"] == "operator"


@pytest.mark.asyncio
async def test_create_session_invalid_role_fallback():
    """非法角色（如 admin）由 Pydantic 校验拒绝（422），不再静默回退。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sessions", json={"role": "admin"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_sessions_shows_user():
    """会话列表应返回每个会话的 user 身份。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions", json={"role": "finance", "user_id": "fin_01"})
        resp = await client.get("/sessions")
        data = resp.json()
        assert data[0]["user"]["role"] == "finance"
        assert data[0]["user"]["user_id"] == "fin_01"