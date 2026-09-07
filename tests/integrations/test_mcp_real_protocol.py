"""真实 MCP 协议测试 — 连接外部工具模拟服务，走完整 stdio / JSON-RPC 流程。

覆盖：子进程拉起、initialize 握手、tools/list 发现、tools/call 调用、
未知工具错误、并发调用、关闭释放。全部经真实协议，无 mock 替代。

MCP 服务定位为「外部工具模拟」通道（汇率/天气演示工具）；
电商运营工具走内置平台 API 通道（见 test_batch4_tools.py）。
"""

import asyncio

import pytest

from integrations.mcp.client_pool import McpClientPool


def _run(coro):
    return asyncio.run(coro)


def test_mcp_real_connect_and_discover():
    """真实连接 + tools/list 发现外部演示工具。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        assert pool.connected is True
        defs = pool.get_all_tool_definitions()
        names = [d["name"] for d in defs]
        assert {"query_exchange_rate", "query_weather"} <= set(names)
        # 每个定义均为 OpenAI function calling 结构
        for d in defs:
            assert "name" in d and "description" in d and "parameters" in d
            assert d["parameters"].get("type") == "object"
        await pool.close()
        assert pool.connected is False

    _run(scenario())


def test_mcp_real_call_tool():
    """真实 tools/call：两个演示工具均返回结果文本。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            r1 = await pool.call_tool("query_exchange_rate", {"currency": "USD"})
            assert isinstance(r1, str) and "7.18" in r1
            r2 = await pool.call_tool("query_weather", {"city": "杭州"})
            assert isinstance(r2, str) and "晴" in r2
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
            assert "query_exchange_rate" in handlers
            result = await handlers["query_exchange_rate"](currency="EUR")
            assert isinstance(result, str) and "7.82" in result
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
    """并发 tools/call：多工具混合并发均成功。"""

    async def scenario():
        pool = McpClientPool()
        await pool.connect()
        try:
            results = await asyncio.gather(
                pool.call_tool("query_exchange_rate", {"currency": "USD"}),
                pool.call_tool("query_exchange_rate", {"currency": "JPY"}),
                pool.call_tool("query_weather", {"city": "上海"}),
            )
            assert all(isinstance(r, str) and r for r in results)
        finally:
            await pool.close()

    _run(scenario())