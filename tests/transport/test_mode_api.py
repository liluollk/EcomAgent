"""权限模式 API 测试 — 创建会话带模式、GET/PUT 模式、非法模式校验。"""

import os
import pytest
from httpx import ASGITransport, AsyncClient
from transport.server import app, sessions, workspaces


@pytest.fixture(autouse=True)
def cleanup_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path / "sessions"))
    sessions.clear()
    workspaces.clear()
    yield
    sessions.clear()
    workspaces.clear()


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_create_session_defaults_to_ask():
    """无 body 创建：默认 ASK 模式，响应含 permission_mode。"""
    async with await _client() as client:
        resp = await client.post("/sessions")
        assert resp.status_code == 200
        assert resp.json()["permission_mode"] == "ASK"


async def test_create_session_with_mode():
    """创建会话可指定初始权限模式。"""
    async with await _client() as client:
        resp = await client.post("/sessions", json={"permission_mode": "EXECUTE"})
        assert resp.status_code == 200
        assert resp.json()["permission_mode"] == "EXECUTE"


async def test_create_session_invalid_mode_422():
    """非法模式名由 Pydantic 校验拒绝。"""
    async with await _client() as client:
        resp = await client.post("/sessions", json={"permission_mode": "nope"})
        assert resp.status_code == 422


async def test_get_session_mode():
    async with await _client() as client:
        sid = (await client.post("/sessions", json={"permission_mode": "READONLY"})).json()["session_id"]
        resp = await client.get(f"/sessions/{sid}/mode")
        assert resp.status_code == 200
        assert resp.json()["mode"] == "READONLY"


async def test_put_session_mode():
    async with await _client() as client:
        sid = (await client.post("/sessions")).json()["session_id"]
        resp = await client.put(f"/sessions/{sid}/mode", json={"mode": "EXECUTE"})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "EXECUTE"
        # 持久化后仍可读回
        got = (await client.get(f"/sessions/{sid}/mode")).json()
        assert got["mode"] == "EXECUTE"


async def test_put_session_mode_invalid_rejected():
    """非法模式名由 SetSessionModeRequest 的 Literal 校验拒绝（422）。"""
    async with await _client() as client:
        sid = (await client.post("/sessions")).json()["session_id"]
        resp = await client.put(f"/sessions/{sid}/mode", json={"mode": "weird"})
        assert resp.status_code == 422


async def test_mode_endpoints_missing_session_404():
    async with await _client() as client:
        assert (await client.get("/sessions/nope/mode")).status_code == 404
        assert (await client.put("/sessions/nope/mode", json={"mode": "ASK"})).status_code == 404