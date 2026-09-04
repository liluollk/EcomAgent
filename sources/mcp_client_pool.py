"""
MCP 客户端池 — 管理 MCP Server 连接生命周期与工具发现、调用。

两层模式：
1. 真实 MCP 模式（default）：通过 mcp SDK 的 stdio_client + ClientSession
   连接本地 MCP Server（mocks/mcp_tool_server.py），执行 initialize 握手、
   tools/list 发现、tools/call 调用，协议全链路真实（JSON-RPC over stdio）。
2. 内存 Source 模式（fallback / 测试）：直接注册 Source 对象模拟工具发现，
   连接不可用时兜底，保证未连接状态下服务仍可用。

真实模式下 get_all_tool_definitions / get_all_handlers 返回协议发现结果，
未连接时回退内存注册的 Source。
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sources.source import Source, Tool


def default_server_params() -> StdioServerParameters:
    """默认 MCP Server 启动参数：以子进程方式拉起本地 MCP Server。

    显式传递当前进程环境，保证 CHANNEL_API_URL 等配置被 MCP Server
    子进程继承（REST 渠道层依赖）。
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mocks.mcp_tool_server"],
        env=dict(os.environ),
    )


class McpClientPool:
    """MCP 客户端池，管理 MCP Server 连接与工具发现。

    用法（真实模式）:
        pool = McpClientPool()
        await pool.connect()
        defs = pool.get_all_tool_definitions()
        handlers = pool.get_all_handlers()
        result = await pool.call_tool("query_inventory", {"channel": "taobao", "sku": "SKU-001"})
        await pool.close()

    用法（内存模式，未连接时自动回退）:
        pool = McpClientPool()
        pool.register_source(taobao_source)
    """

    def __init__(self) -> None:
        """初始化客户端池（内存 Source 注册表 + 连接状态）。"""
        self._sources: dict[str, Source] = {}
        self._client: Optional[tuple] = None  # (read, write) stdio 流
        self._session: Optional[ClientSession] = None
        self._live_tool_defs: list[dict] = []
        self._live_names: list[str] = []
        self._connected: bool = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 连接生命周期（真实 MCP 协议）
    # ------------------------------------------------------------------

    async def connect(self, params: Optional[StdioServerParameters] = None) -> None:
        """建立真实 MCP 连接：stdio 传输 → initialize 握手 → tools/list 发现。

        Args:
            params: StdioServerParameters，默认拉起 mocks.mcp_tool_server 子进程。

        Raises:
            RuntimeError: 连接或握手失败。
        """
        if self._connected:
            return
        params = params or default_server_params()
        try:
            self._client = stdio_client(params)
            read, write = await self._client.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
            tools = await self._session.list_tools()
            self._live_names = [t.name for t in tools.tools]
            self._live_tool_defs = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": (t.inputSchema or {"type": "object"}),
                }
                for t in tools.tools
            ]
            self._connected = True
        except Exception as exc:
            await self.close()
            raise RuntimeError(f"MCP 连接失败: {exc}") from exc

    async def close(self) -> None:
        """关闭 MCP 连接，释放子进程与 stdio 流。"""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """是否已建立真实 MCP 连接。"""
        return self._connected

    # ------------------------------------------------------------------
    # 工具调用（真实 MCP 协议 tools/call）
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """通过 MCP 协议调用工具（tools/call）。

        Args:
            tool_name: 工具名。
            arguments: 工具参数。

        Returns:
            str: 工具结果文本。

        Raises:
            RuntimeError: 未连接或调用失败。
        """
        if not self._connected or self._session is None:
            raise RuntimeError("MCP 未连接，无法调用工具")
        async with self._lock:
            result = await self._session.call_tool(tool_name, arguments)
        if result.isError:
            texts = _extract_text(result)
            raise RuntimeError(f"MCP 工具执行失败 {tool_name}: {texts}")
        return _extract_text(result)

    # ------------------------------------------------------------------
    # 内存 Source 注册（fallback / 测试）
    # ------------------------------------------------------------------

    def register_source(self, source: Source) -> None:
        """注册一个 Source 到客户端池。"""
        self._sources[source.name] = source

    def unregister_source(self, source_name: str) -> None:
        """注销一个 Source。"""
        self._sources.pop(source_name, None)

    def get_source(self, source_name: str) -> Optional[Source]:
        """获取指定名称的 Source。"""
        return self._sources.get(source_name)

    def get_all_sources(self) -> dict[str, Source]:
        """获取所有已注册的 Source 字典副本。"""
        return dict(self._sources)

    # ------------------------------------------------------------------
    # 工具发现与处理器映射（真实连接优先，内存兜底）
    # ------------------------------------------------------------------

    def get_all_tool_definitions(self) -> list[dict]:
        """获取工具定义（OpenAI function calling 格式）。

        已连接时返回 MCP tools/list 发现的真实定义，否则返回内存 Source 定义。
        """
        if self._connected:
            return list(self._live_tool_defs)
        all_tools = []
        for source in self._sources.values():
            for tool in source.tools:
                all_tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                })
        return all_tools

    def get_all_handlers(self) -> dict[str, Callable[..., Any]]:
        """获取工具名 → 处理函数 映射。

        已连接时返回 MCP call_tool 的异步包装（awaitable），
        未连接时返回内存 Source 的 handler（同步或异步均可）。

        调用方应使用 inspect.iscoroutinefunction 判断是否需要 await。
        """
        if self._connected:
            return {name: self._make_live_handler(name) for name in self._live_names}
        handlers = {}
        for source in self._sources.values():
            for tool in source.tools:
                if tool.handler is not None:
                    handlers[tool.name] = tool.handler
        return handlers

    def get_handler(self, tool_name: str) -> Optional[Callable[..., Any]]:
        """根据工具名称获取处理函数（真实连接返回异步包装，否则内存 handler）。

        调用方应使用 inspect.iscoroutinefunction 判断是否需要 await。
        """
        if self._connected:
            if tool_name in self._live_names:
                return self._make_live_handler(tool_name)
            return None
        for source in self._sources.values():
            handler = source.get_tool_handler(tool_name)
            if handler is not None:
                return handler
        return None

    def _make_live_handler(self, tool_name: str) -> Callable:
        """构造 MCP 调用的异步包装：签名与内存 handler 一致 (**kwargs)。"""

        async def _live(**kwargs: Any) -> str:
            return await self.call_tool(tool_name, kwargs)

        _live.__name__ = f"mcp_{tool_name}"
        return _live


def _extract_text(result: Any) -> str:
    """从 MCP CallToolResult 提取文本内容。"""
    if result.content is None:
        return ""
    parts = []
    for block in result.content:
        if getattr(block, "type", "") == "text" and getattr(block, "text", None):
            parts.append(block.text)
    return "\n".join(parts)