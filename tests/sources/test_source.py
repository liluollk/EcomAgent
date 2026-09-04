"""测试 Source — 工具定义和管理。"""

import pytest
from sources.source import Source, Tool


def test_source_creation():
    source = Source(name="taobao", description="淘宝开放平台", type="mcp")
    assert source.name == "taobao"
    assert source.type == "mcp"
    assert len(source.tools) == 0


def test_source_add_tool():
    source = Source(name="taobao", description="淘宝")
    tool = Tool(
        name="query_inventory",
        description="查询库存",
        parameters={"type": "object", "properties": {}},
    )
    source.add_tool(tool)
    assert len(source.tools) == 1
    assert source.get_tool_handler("query_inventory") is None  # handler 未设置


def test_source_get_tool_handler():
    def handler(**kwargs):
        return "mock"
    source = Source(name="taobao", description="淘宝")
    tool = Tool(
        name="query_inventory",
        description="查询库存",
        parameters={},
        handler=handler,
    )
    source.add_tool(tool)
    assert source.get_tool_handler("query_inventory") is handler
    assert source.get_tool_handler("nonexistent") is None


def test_source_get_tool_definitions():
    source = Source(name="taobao", description="淘宝")
    source.add_tool(Tool(
        name="query_inventory",
        description="查询库存",
        parameters={"type": "object"},
    ))
    source.add_tool(Tool(
        name="update_price",
        description="更新价格",
        parameters={"type": "object"},
    ))
    defs = source.get_tool_definitions()
    assert len(defs) == 2
    assert defs[0]["name"] == "query_inventory"
    assert defs[1]["name"] == "update_price"