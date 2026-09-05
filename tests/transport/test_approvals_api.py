"""审批中心 API 测试 — 跨会话权限聚合 / 决定唤醒 / 历史。"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from transport.state import APPROVAL_REGISTRY, sessions
from transport.server import app
from session.session import PermissionMode, Session
from session.workspace import Workspace


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """隔离审批注册表、会话表与存储目录。"""
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path / "sessions"))
    APPROVAL_REGISTRY.clear()
    sessions.clear()
    yield
    APPROVAL_REGISTRY.clear()
    sessions.clear()


def _make_session(sid: str = "s1") -> Session:
    ws = Workspace(workspace_id="w1", name="OceanBreeze")
    session = Session(
        session_id=sid,
        workspace=ws,
        permission_mode=PermissionMode.ASK,
        active_sources=["taobao"],
        user={"user_id": "u1", "role": "manager"},
    )
    sessions[sid] = session
    return session


def _register(session_id: str, request_id: str, future: asyncio.Future) -> None:
    APPROVAL_REGISTRY[request_id] = {
        "future": future,
        "session_id": session_id,
        "tool_name": "update_price",
        "tool_input": {"channel": "taobao", "sku": "SKU-001", "price": 79},
        "reason": "改价为高危写操作",
        "timestamp": 100.0,
    }


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_pending_lists_undecided_requests():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _register("s1", "perm_1", future)

    async with await _client() as client:
        r = await client.get("/approvals/pending")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["pending"][0]["request_id"] == "perm_1"
        assert body["pending"][0]["tool_name"] == "update_price"

    # 已决（future 完成）的条目不再出现在待审批列表
    future.set_result(True)
    async with await _client() as client:
        r = await client.get("/approvals/pending")
        assert r.json()["total"] == 0


async def test_decision_wakes_future_and_updates_session(tmp_path):
    session = _make_session("s1")
    session.permission_requests.append(
        {
            "request_id": "perm_1",
            "tool_name": "update_price",
            "input": {"channel": "taobao", "sku": "SKU-001", "price": 79},
            "user": {"user_id": "u1", "role": "manager"},
            "timestamp": 100.0,
        }
    )
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _register("s1", "perm_1", future)

    async with await _client() as client:
        r = await client.post(
            "/approvals/perm_1/decision", json={"approved": True}
        )
        assert r.status_code == 200
        assert r.json() == {"request_id": "perm_1", "approved": True}

    # 挂起的 turn 被唤醒且结果正确；会话权限记录已写回
    assert await future is True
    assert session.permission_requests[0]["approved"] is True

    # 不存在的请求 → 404
    async with await _client() as client:
        r = await client.post(
            "/approvals/perm_missing/decision", json={"approved": False}
        )
        assert r.status_code == 404


async def test_history_returns_only_decided_records():
    session = _make_session("s1")
    session.permission_requests.append(
        {
            "request_id": "perm_done",
            "tool_name": "update_price",
            "input": {"price": 79},
            "user": {"user_id": "u1", "role": "manager"},
            "timestamp": 100.0,
            "approved": True,
        }
    )
    session.permission_requests.append(
        {
            "request_id": "perm_hanging",
            "tool_name": "product_shelf",
            "input": {"status": "off"},
            "user": {"user_id": "u1", "role": "manager"},
            "timestamp": 101.0,
        }
    )

    async with await _client() as client:
        r = await client.get("/approvals/history")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["history"][0]["request_id"] == "perm_done"
        assert body["history"][0]["approved"] is True
