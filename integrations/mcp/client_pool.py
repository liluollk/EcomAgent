"""MCP 客户端池 — 管理多个 MCP Server 连接生命周期与工具发现、调用。"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sources.source import Source, Tool


def default_server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mocks.mcp_tool_server"],
        env=dict(os.environ),
    )


class _ServerConn:
    def __init__(self, name: str, params: StdioServerParameters) -> None:
        self.name = name
        self.params = params
        self.client: Optional[tuple] = None
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
    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._servers: dict[str, _ServerConn] = {}
        self._tool_routes: dict[str, str] = {}

    async def connect(self, params: Optional[StdioServerParameters] = None) -> None:
        if self._servers:
            return
        if params is not None:
            targets = [("default", params)]
        else:
            from integrations.mcp.server_config import DEFAULT_MCP_SERVER_REGISTRY, to_params

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
        for conn in reversed(list(self._servers.values())):
            await conn.shut()
        self._servers = {}
        self._tool_routes = {}

    @property
    def connected(self) -> bool:
        return any(c.connected for c in self._servers.values())

    def server_status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "connected": c.connected,
                "tools": [{"name": t["name"], "description": t.get("description", "")} for t in c.tool_defs],
            }
            for c in self._servers.values()
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
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

    def register_source(self, source: Source) -> None:
        self._sources[source.name] = source

    def unregister_source(self, source_name: str) -> None:
        self._sources.pop(source_name, None)

    def get_source(self, source_name: str) -> Optional[Source]:
        return self._sources.get(source_name)

    def get_all_sources(self) -> dict[str, Source]:
        return dict(self._sources)

    def get_all_tool_definitions(self) -> list[dict]:
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
        if self._servers:
            return {name: self._make_live_handler(name) for name in self._tool_routes}
        handlers = {}
        for source in self._sources.values():
            for tool in source.tools:
                if tool.handler is not None:
                    handlers[tool.name] = tool.handler
        return handlers

    def get_handler(self, tool_name: str) -> Optional[Callable[..., Any]]:
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
        async def _live(**kwargs: Any) -> str:
            return await self.call_tool(tool_name, kwargs)

        _live.__name__ = f"mcp_{tool_name}"
        return _live


def _extract_text(result: Any) -> str:
    if result.content is None:
        return ""
    parts = []
    for block in result.content:
        if getattr(block, "type", "") == "text" and getattr(block, "text", None):
            parts.append(block.text)
    return "\n".join(parts)