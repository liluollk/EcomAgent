"""MCP 路由测试 — /mcp/status 状态聚合 + /mcp/servers 配置 CRUD。"""

import pytest
from httpx import ASGITransport, AsyncClient

import integrations.mcp.server_config as msc
from transport.server import app


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    """隔离运行时目录与 MCP server 配置，避免污染 data/。"""
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("CHANNEL_API_AUTO", "0")
    monkeypatch.setenv("MCP_SERVERS_CONFIG_FILE", str(tmp_path / "mcp_servers.json"))
    msc._store._mtime = -1
    msc._store._servers = None


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_mcp_status_shape():
    async with await _client() as client:
        resp = await client.get("/mcp/status")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["connected"], bool)
        assert data["transport"] == "stdio / JSON-RPC"
        assert isinstance(data["tools"], list)
        assert isinstance(data["servers"], list)
        # /mcp/status 反映 MCP 外部工具通道：已连接时含演示工具，未连接时为空清单
        for t in data["tools"]:
            assert t["name"] and isinstance(t["description"], str)
        if data["connected"]:
            names = {t["name"] for t in data["tools"]}
            assert {"query_exchange_rate", "query_weather"} <= names


async def test_mcp_servers_crud():
    async with await _client() as client:
        # 默认列表（external-demo）
        resp = await client.get("/mcp/servers")
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert "external-demo" in names

        # 新增
        resp = await client.post("/mcp/servers", json={
            "name": "user-tools", "command": "python", "args": ["-m", "some.server"],
            "env": {"API_KEY": "secret"},
        })
        assert resp.status_code == 200
        # env 值掩码
        assert resp.json()["env"]["API_KEY"] == "****"

        # 重名 400
        resp = await client.post("/mcp/servers", json={"name": "user-tools", "command": "python"})
        assert resp.status_code == 400

        # 停用
        resp = await client.patch("/mcp/servers/user-tools", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # 删除
        resp = await client.delete("/mcp/servers/user-tools")
        assert resp.status_code == 200
        resp = await client.get("/mcp/servers")
        assert "user-tools" not in {s["name"] for s in resp.json()}

        # 删除不存在 → 404
        resp = await client.delete("/mcp/servers/nope")
        assert resp.status_code == 404


async def test_mcp_server_test_endpoint():
    """连通性测试：对默认演示 server 握手成功并返回工具清单。"""
    async with await _client() as client:
        resp = await client.post("/mcp/servers/external-demo/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True, data.get("error")
        names = {t["name"] for t in data["tools"]}
        assert {"query_exchange_rate", "query_weather"} <= names