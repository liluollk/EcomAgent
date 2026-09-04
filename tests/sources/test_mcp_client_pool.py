"""测试 McpClientPool — 注册、注销和工具发现。"""

import pytest
from sources.source import Source, Tool
from sources.mcp_client_pool import McpClientPool


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