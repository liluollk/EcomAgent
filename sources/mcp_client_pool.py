"""
MCP 客户端池 — 管理多个 MCP Server 连接生命周期与工具发现、调用。

两层模式：
1. 真实 MCP 模式（default）：通过 mcp SDK 的 stdio_client + ClientSession
   连接配置注册表（sources/mcp_server_config.py，data/mcp_servers.json）中
   全部启用的 MCP Server，执行 initialize 握手、tools/list 发现、tools/call
   调用，协议全链路真实（JSON-RPC over stdio）。
2. 内存 Source 模式（fallback / 测试）：直接注册 Source 对象模拟工具发现，
   连接不可用时兜底，保证未连接状态下服务仍可用。

多 server 语义：
- 工具发现结果跨 server 合并，同名工具先到先得（先连接的 server 优先）；
- call_tool 按 tool_name → server 路由表分发，每 server 独立锁串行化；
- connect(params=...) 显式传参时按单 server 连接（测试/定制场景）。
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
    """默认 MCP Server 启动参数：以子进程方式拉起本地演示服务。

    显式传递当前进程环境，保证 CHANNEL_API_URL 等配置被 MCP Server
    子进程继承（REST 渠道层依赖）。
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mocks.mcp_tool_server"],
        env=dict(os.environ),
    )


class _ServerConn:
    """单个 MCP Server 的连接记录（stdio 流 + session + 发现结果 + 串行锁）。"""

    def __init__(self, name: str, params: StdioServerParameters) -> None:
        self.name = name
        self.params = params
        self.client: Optional[tuple] = None  # (read, write) stdio 流
        self.session: Optional[ClientSession] = None
        self.tool_defs: list[dict] = []
        self.tool_names: list[str] = []
        self.connected: bool = False
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.client = stdio_client(self.params)
        read, write = await self.client.__aenter__()
        self.session = ClientSession(read, write)
        await self.session.__aenter__()
        await self.session.initialize()
        tools = await self.session.list_tools()
        self.tool_names = [t.name for t in tools.tools]
        self.tool_defs = [
            {
                "name": t.name,
                "description": t.description or "",
                "parameters": (t.inputSchema or {"type": "object"}),
            }
            for t in tools.tools
        ]
        self.connected = True

    async def shut(self) -> None:
        if self.session is not None:
            try:
                await self.session.__aexit__(None, None, None)
            except Exception:
                pass
            self.session = None
        if self.client is not None:
            try:
                await self.client.__aexit__(None, None, None)
            except Exception:
                pass
            self.client = None
        self.connected = False
        self.tool_defs = []
        self.tool_names = []


class McpClientPool:
    """MCP 客户端池，管理多 MCP Server 连接与工具发现。

    用法（真实模式，连接配置注册表全部启用 server）:
        pool = McpClientPool()
        await pool.connect()
        defs = pool.get_all_tool_definitions()
        handlers = pool.get_all_handlers()
        result = await pool.call_tool("query_exchange_rate", {"currency": "USD"})
        await pool.close()

    用法（单 server 显式连接，测试/定制）:
        await pool.connect(params=default_server_params())

    用法（内存模式，未连接时自动回退）:
        pool.register_source(some_source)
    """

    def __init__(self) -> None:
        """初始化客户端池（内存 Source 注册表 + 多 server 连接记录）。"""
        self._sources: dict[str, Source] = {}
        self._servers: dict[str, _ServerConn] = {}  # server name → conn（保持连接顺序）
        self._tool_routes: dict[str, str] = {}  # tool name → server name（先到先得）

    # ------------------------------------------------------------------
    # 连接生命周期（真实 MCP 协议，多 server）
    # ------------------------------------------------------------------

    async def connect(self, params: Optional[StdioServerParameters] = None) -> None:
        """建立真实 MCP 连接。

        Args:
            params: 显式 StdioServerParameters 时按单 server（名为 default）
                连接；缺省读取配置注册表（data/mcp_servers.json）连接全部
                启用 server。

        Raises:
            RuntimeError: 任一 server 连接或握手失败（已建立的连接全部回收）。
        """
        if self._servers:
            return
        if params is not None:
            targets = [("default", params)]
        else:
            from sources.mcp_server_config import DEFAULT_MCP_SERVER_REGISTRY, to_params

            targets = [
                (str(cfg.get("name") or f"server-{i}"), to_params(cfg))
                for i, cfg in enumerate(DEFAULT_MCP_SERVER_REGISTRY.enabled_configs())
            ]
        opened: list[_ServerConn] = []
        try:
            for name, p in targets:
                conn = _ServerConn(name, p)
                await conn.open()
                opened.append(conn)
                self._servers[name] = conn
                for t in conn.tool_names:
                    self._tool_routes.setdefault(t, name)
        except Exception as exc:
            for conn in reversed(opened):
                await conn.shut()
            self._servers = {}
            self._tool_routes = {}
            raise RuntimeError(f"MCP 连接失败: {exc}") from exc

    async def close(self) -> None:
        """关闭全部 MCP 连接，释放子进程与 stdio 流。

        按连接逆序（LIFO）关闭：stdio_client 的 anyio cancel scope 必须
        嵌套退出，顺序关闭会触发 cancel scope 冲突。
        """
        for conn in reversed(list(self._servers.values())):
            await conn.shut()
        self._servers = {}
        self._tool_routes = {}

    @property
    def connected(self) -> bool:
        """是否已建立至少一个真实 MCP 连接。"""
        return any(c.connected for c in self._servers.values())

    def server_status(self) -> list[dict[str, Any]]:
        """各 server 的连接状态与工具清单（/mcp/status 消费）。"""
        return [
            {
                "name": c.name,
                "connected": c.connected,
                "tools": [{"name": t["name"], "description": t.get("description", "")} for t in c.tool_defs],
            }
            for c in self._servers.values()
        ]

    # ------------------------------------------------------------------
    # 工具调用（真实 MCP 协议 tools/call，按路由表分发）
    # ------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """通过 MCP 协议调用工具（tools/call），路由到持有该工具的 server。

        Args:
            tool_name: 工具名。
            arguments: 工具参数。

        Returns:
            str: 工具结果文本。

        Raises:
            RuntimeError: 未连接或工具无路由。
        """
        server_name = self._tool_routes.get(tool_name)
        conn = self._servers.get(server_name) if server_name else None
        if conn is None or not conn.connected or conn.session is None:
            raise RuntimeError(f"MCP 未连接或工具不存在: {tool_name}")
        async with conn.lock:
            result = await conn.session.call_tool(tool_name, arguments)
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
    # 工具发现与处理器映射（真实连接优先合并，内存兜底）
    # ------------------------------------------------------------------

    def get_all_tool_definitions(self) -> list[dict]:
        """获取工具定义（OpenAI function calling 格式）。

        已连接时合并全部 server 的发现结果（同名先到先得），
        否则返回内存 Source 定义。
        """
        if self._servers:
            merged: list[dict] = []
            seen: set[str] = set()
            for conn in self._servers.values():
                for d in conn.tool_defs:
                    if d["name"] not in seen:
                        seen.add(d["name"])
                        merged.append(dict(d))
            return merged
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
        if self._servers:
            return {name: self._make_live_handler(name) for name in self._tool_routes}
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
        if self._servers:
            if tool_name in self._tool_routes:
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