"""真实 MCP 协议测试 — 连接本地 MCP Server，走完整 stdio / JSON-RPC 流程。

覆盖：子进程拉起、initialize 握手、tools/list 发现、tools/call 调用、
未知工具错误、并发调用、关闭释放。全部经真实协议，无 mock 替代。
"""

import asyncio

import pytest

from sources.mcp_client_pool import McpClientPool


def _run(coro):
    return asyncio.run(coro)


def test_mcp_real_connect_and_discover():
    """真实连接 + tools/list 发现 4 个渠道工具。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        assert pool.connected is True
        defs = pool.get_all_tool_definitions()
        names = [d["name"] for d in defs]
        assert {"query_inventory", "update_price", "create_promotion", "query_order_status"} <= set(names)
        # 每个定义均为 OpenAI function calling 结构
        for d in defs:
            assert "name" in d and "description" in d and "parameters" in d
            assert d["parameters"].get("type") == "object"
        await pool.close()
        assert pool.connected is False

    _run(scenario())


def test_mcp_real_call_tool():
    """真实 tools/call：读取与写操作均返回结果文本。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            r1 = await pool.call_tool("query_inventory", {"channel": "taobao", "sku": "SKU-001"})
            assert isinstance(r1, str) and len(r1) > 0
            r2 = await pool.call_tool(
                "update_price",
                {"channel": "jd", "sku": "SKU-001", "new_price": 89.0},
            )
            assert isinstance(r2, str) and "89" in r2
        finally:
            await pool.close()

    _run(scenario())


def test_mcp_real_handlers_wrapper():
    """get_all_handlers 返回的异步包装等价于 call_tool。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            handlers = pool.get_all_handlers()
            assert "query_inventory" in handlers
            result = await handlers["query_inventory"](channel="taobao", sku="SKU-001")
            assert isinstance(result, str) and len(result) > 0
            assert pool.get_handler("no_such_tool") is None
        finally:
            await pool.close()

    _run(scenario())


def test_mcp_real_call_unknown_tool_raises():
    """未知工具经真实协议返回错误（client 抛异常）。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            with pytest.raises(Exception):
                await pool.call_tool("no_such_tool", {})
        finally:
            await pool.close()

    _run(scenario())


def test_mcp_real_concurrent_calls():
    """并发 tools/call：读/写混合并发均成功。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            results = await asyncio.gather(
                pool.call_tool("query_inventory", {"channel": "taobao", "sku": "SKU-001"}),
                pool.call_tool("query_order_status", {"channel": "douyin", "order_id": "ORD-001"}),
                pool.call_tool(
                    "create_promotion",
                    {
                        "channel": "taobao",
                        "sku": "SKU-001",
                        "discount": 0.8,
                        "start_time": "2026-01-01",
                        "end_time": "2026-01-03",
                    },
                ),
            )
            assert all(isinstance(r, str) and r for r in results)
        finally:
            await pool.close()

    _run(scenario())