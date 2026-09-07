"""测试 McpClientPool — 注册、注销、工具发现与多 server 合并路由。"""

import asyncio

import pytest
from sources.source import Source, Tool
from integrations.mcp.client_pool import McpClientPool


def test_mcp_pool_register_source():
    pool = McpClientPool()
    source = Source(name="taobao", description="淘宝")
    pool.register_source(source)
    assert pool.get_source("taobao") is source


def test_mcp_pool_unregister_source():
    pool = McpClientPool()
    source = Source(name="taobao", description="淘宝")
    pool.register_source(source)
    pool.unregister_source("taobao")
    assert pool.get_source("taobao") is None


def test_mcp_pool_get_all_tool_definitions():
    pool = McpClientPool()
    source = Source(name="taobao", description="淘宝")
    source.add_tool(Tool(
        name="query_inventory",
        description="查询库存",
        parameters={"type": "object"},
    ))
    pool.register_source(source)
    defs = pool.get_all_tool_definitions()
    assert len(defs) == 1
    assert defs[0]["name"] == "query_inventory"


def test_mcp_pool_get_all_handlers():
    pool = McpClientPool()
    source = Source(name="taobao", description="淘宝")
    source.add_tool(Tool(
        name="query_inventory",
        description="查询库存",
        parameters={},
        handler=lambda **kw: "mock",
    ))
    pool.register_source(source)
    handlers = pool.get_all_handlers()
    assert "query_inventory" in handlers
    assert handlers["query_inventory"]() == "mock"


def test_mcp_pool_get_handler():
    pool = McpClientPool()
    source = Source(name="taobao", description="淘宝")
    source.add_tool(Tool(
        name="query_inventory",
        description="查询库存",
        parameters={},
        handler=lambda **kw: "mock",
    ))
    pool.register_source(source)
    handler = pool.get_handler("query_inventory")
    assert handler is not None
    assert handler() == "mock"
    assert pool.get_handler("nonexistent") is None


def test_mcp_pool_get_all_sources():
    pool = McpClientPool()
    source = Source(name="taobao", description="淘宝")
    pool.register_source(source)
    sources = pool.get_all_sources()
    assert len(sources) == 1
    assert "taobao" in sources


def test_mcp_pool_multi_server_merge_and_route(tmp_path, monkeypatch):
    """配置两个 server（同一演示服务、不同名）：工具合并去重、调用路由正常。"""
    cfg = tmp_path / "mcp_servers.json"
    cfg.write_text(
        '{"servers": ['
        '{"name": "demo-a", "command": "%s", "args": ["-m", "mocks.mcp_tool_server"], "env": {}, "enabled": true},'
        '{"name": "demo-b", "command": "%s", "args": ["-m", "mocks.mcp_tool_server"], "env": {}, "enabled": true}'
        "]}" % (__import__("sys").executable.replace("\\", "\\\\"), __import__("sys").executable.replace("\\", "\\\\")),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVERS_CONFIG_FILE", str(cfg))
    import integrations.mcp.server_config as _msc

    _msc._store._mtime = -1
    _msc._store._servers = None

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            assert pool.connected
            status = pool.server_status()
            assert {s["name"] for s in status} == {"demo-a", "demo-b"}
            defs = pool.get_all_tool_definitions()
            names = [d["name"] for d in defs]
            # 两个 server 工具集相同 → 合并去重（先到先得）
            assert len(names) == len(set(names))
            assert {"query_exchange_rate", "query_weather"} <= set(names)
            r = await pool.call_tool("query_exchange_rate", {"currency": "USD"})
            assert "7.18" in r
        finally:
            await pool.close()
        assert pool.connected is False

    asyncio.run(scenario())