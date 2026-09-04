"""测试 FastAPI 传输层 — REST API 和 WebSocket。"""

import json
import os
import pytest
from httpx import ASGITransport, AsyncClient
from transport.server import app, sessions, workspaces


@pytest.fixture(autouse=True)
def cleanup_sessions():
    """每个测试前清理全局会话、空间与测试持久化目录。"""
    sessions.clear()
    workspaces.clear()
    storage_dir = os.environ.get("AGENT_STORAGE_DIR")
    if storage_dir and os.path.isdir(storage_dir):
        for fname in os.listdir(storage_dir):
            if fname.endswith(".jsonl"):
                os.remove(os.path.join(storage_dir, fname))
    yield


@pytest.mark.asyncio
async def test_create_session():
    """创建会话后返回 session_id 和 ACTIVE 状态。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "ACTIVE"


@pytest.mark.asyncio
async def test_list_sessions():
    """创建两个会话后，列表应返回 2 条。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/sessions")
        await client.post("/sessions")
        resp = await client.get("/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2


@pytest.mark.asyncio
async def test_delete_session():
    """删除存在的会话返回 200，删除不存在的返回 404。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 创建会话
        resp = await client.post("/sessions")
        sid = resp.json()["session_id"]

        # 删除会话
        resp = await client.delete(f"/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == sid

        # 再次删除应返回 404
        resp = await client.delete(f"/sessions/{sid}")
        assert resp.status_code == 404

        # 删除不存在的会话
        resp = await client.delete("/sessions/nonexistent")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_root_returns_html_fallback():
    """根路径返回 HTML（React 构建产物不存在时 fallback 到 static/index.html）。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
        # 如果 React 构建产物不存在且 static/index.html 也不存在，返回 404 JSON
        # 否则返回 200 HTML
        assert resp.status_code in (200, 404)


@pytest.mark.asyncio
async def test_get_session_messages_empty():
    """新会话的消息历史为空列表。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sessions")
        sid = resp.json()["session_id"]
        resp = await client.get(f"/sessions/{sid}/messages")
        assert resp.status_code == 200
        assert resp.json() == []


@pytest.mark.asyncio
async def test_get_messages_restores_tool_structure_from_disk():
    """内存中不存在的会话应从磁盘恢复，并返回含 tool_calls 结构的完整历史。"""
    storage_dir = os.environ.get("AGENT_STORAGE_DIR")
    assert storage_dir, "conftest 应设置 AGENT_STORAGE_DIR"
    sid = "restore01"
    lines = [
        {"role": "user", "content": "查询库存", "timestamp": "t0"},
        {
            "role": "assistant", "content": "", "timestamp": "t1",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "query_inventory", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "库存 100", "timestamp": "t2"},
        {"role": "assistant", "content": "库存为 100", "timestamp": "t3"},
        {
            "record": "tool_call", "tool_name": "query_inventory", "tool_use_id": "c1",
            "input": {}, "result": "库存 100", "is_error": False, "timestamp": "t2",
        },
    ]
    os.makedirs(storage_dir, exist_ok=True)
    with open(os.path.join(storage_dir, f"{sid}.jsonl"), "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/sessions/{sid}/messages")
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 4, "审计记录不应出现在消息历史中"
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "query_inventory"
        assert msgs[2]["tool_call_id"] == "c1"

        # 恢复后的会话同时出现在会话列表中
        resp = await client.get("/sessions")
        assert any(s["session_id"] == sid for s in resp.json())


@pytest.mark.asyncio
async def test_get_messages_unknown_session_returns_404():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/sessions/ghost-session/messages")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_websocket_connect():
    """测试 WebSocket 连接会话 — 使用 ASGI scope 直接测试。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/sessions")
        session_id = resp.json()["session_id"]
        # 通过 ASGI Transport 发送 WebSocket scope 验证路由存在
        # 直接传入 ASGI app 和 scope 进行连接
        from starlette.types import Scope, Receive, Send
        import asyncio

        connected = False
        received_messages = []

        async def receive():
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            await asyncio.sleep(0.1)
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            received_messages.append(message)

        scope: Scope = {
            "type": "websocket",
            "path": f"/ws/{session_id}",
            "headers": [],
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 8000),
            "subprotocols": [],
        }
        await app(scope, receive, send)
        assert len(received_messages) > 0
        assert received_messages[0]["type"] == "websocket.accept"