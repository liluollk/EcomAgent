"""MCP 客户端最小适配层(计划 04 Task 6 Step 3;架构文档 §3.5、§9)。

- ``McpClient`` 协议只暴露 ``list_tools``/``call_tool``;测试与上层一律注入
  fake 实现,本模块不发真实网络请求、不启动子进程;
- ``HttpMcpClient``:远程 HTTPS Source。JSON-RPC 2.0 报文经注入的
  ``McpJsonRpcTransport`` 发送(生产组合根可用 ``HttpxJsonRpcTransport`` 或
  MCP Python SDK 的 streamable-http transport);端点必须已完成
  ``EndpointPolicy`` 校验——入参为 ``NormalizedEndpoint``,无法绕过;
- ``StdioMcpClient``:本地 stdio Source。命令只能由部署级 ``ServerKeyRegistry``
  预注册条目解析(见 adapters.mcp.security),本类不提供、不接受任何外部
  命令参数;stdio 帧传输由组合根注入;
- MCP Server 声明的 effect 只作为 ``declared_effect`` 透传,是否采纳由本地
  受信分类(adapters.mcp.source_sync)决定,客户端与同步层都不信任声明。

MCP Python SDK(``mcp``)在此用于响应报文类型校验。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any, Final, Protocol

import httpx
from mcp.types import (
    CallToolResult as SdkCallToolResult,
)
from mcp.types import (
    ListToolsResult as SdkListToolsResult,
)
from mcp.types import (
    TextContent,
)
from mcp.types import (
    Tool as SdkTool,
)
from mcp.types import (
    ToolAnnotations as SdkToolAnnotations,
)
from pydantic import BaseModel, ConfigDict

from runtime.security.endpoints import NormalizedEndpoint


JSONRPC_VERSION: Final = "2.0"
MCP_PROTOCOL_VERSION: Final = "2025-06-18"

_INITIALIZE_METHOD: Final = "initialize"
_INITIALIZED_NOTIFICATION: Final = "notifications/initialized"
_TOOLS_LIST_METHOD: Final = "tools/list"
_TOOLS_CALL_METHOD: Final = "tools/call"


class McpTransportError(Exception):
    """Source 不可达/超时等传输层失败;调用方可按“源不可用”语义处理。"""


class McpProtocolError(Exception):
    """JSON-RPC 错误响应或报文格式非法;属于确定性失败,不按停机处理。"""


class McpToolDescriptor(BaseModel):
    """MCP Server 声明的工具描述;``declared_*`` 仅透传,本地一律不信任。"""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = {}
    declared_effect: str | None = None
    declared_permissions: frozenset[str] = frozenset()


class McpToolCallResult(BaseModel):
    """``tools/call`` 的归一化结果;``payload`` 为不可信数据。"""

    model_config = ConfigDict(frozen=True)

    payload: object = None
    is_error: bool = False


class McpClient(Protocol):
    """MCP Source 客户端协议;测试注入 fake 实现。"""

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]: ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> McpToolCallResult: ...


class McpJsonRpcTransport(Protocol):
    """一次 JSON-RPC 往返:发送完整请求报文,返回响应 ``result``(可为 None)。"""

    async def request(self, payload: Mapping[str, object]) -> object: ...


def declared_effect_from_annotations(
    annotations: SdkToolAnnotations | None,
) -> str | None:
    """把 MCP Server 的 hint 声明映射为透传字段;不作为本地分类依据。"""
    if annotations is None:
        return None
    if annotations.destructive_hint is True:
        return "WRITE"
    if annotations.read_only_hint is True:
        return "READ"
    return None


def descriptor_from_sdk_tool(tool: SdkTool) -> McpToolDescriptor:
    """SDK ``Tool`` → 本地描述;声明字段仅透传。"""
    return McpToolDescriptor(
        name=tool.name,
        description=tool.description or "",
        input_schema=dict(tool.input_schema) if tool.input_schema else {},
        output_schema=dict(tool.output_schema) if tool.output_schema else {},
        declared_effect=declared_effect_from_annotations(tool.annotations),
        declared_permissions=frozenset(),
    )


def _extract_payload(result: SdkCallToolResult) -> object:
    """优先结构化结果,否则拼接文本内容;原始数据保持不可信语义。"""
    if result.structured_content is not None:
        return result.structured_content
    texts = [item.text for item in result.content if isinstance(item, TextContent)]
    if texts:
        return "\n".join(texts)
    return None


class _JsonRpcSession:
    """共享 JSON-RPC 2.0 会话:initialize 握手 + tools/list、tools/call。"""

    def __init__(self, transport: McpJsonRpcTransport) -> None:
        self._transport = transport
        self._next_id = itertools.count(1)
        self._initialized = False

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        await self._ensure_initialized()
        result = await self._request(_TOOLS_LIST_METHOD, {})
        sdk_result = SdkListToolsResult.model_validate(result)
        return tuple(descriptor_from_sdk_tool(tool) for tool in sdk_result.tools)

    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> McpToolCallResult:
        await self._ensure_initialized()
        result = await self._request(
            _TOOLS_CALL_METHOD, {"name": name, "arguments": dict(arguments)}
        )
        sdk_result = SdkCallToolResult.model_validate(result)
        return McpToolCallResult(
            payload=_extract_payload(sdk_result), is_error=sdk_result.is_error
        )

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        await self._request(
            _INITIALIZE_METHOD,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-runtime", "version": "0.1.0"},
            },
        )
        await self._notify(_INITIALIZED_NOTIFICATION, {})
        self._initialized = True

    async def _request(self, method: str, params: Mapping[str, object]) -> object:
        payload: dict[str, object] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": next(self._next_id),
            "method": method,
            "params": dict(params),
        }
        response = await self._transport.request(payload)
        if not isinstance(response, dict):
            raise McpProtocolError(f"unexpected JSON-RPC response for {method}")
        if response.get("error") is not None:
            raise McpProtocolError(f"JSON-RPC error response for {method}")
        return response.get("result")

    async def _notify(self, method: str, params: Mapping[str, object]) -> None:
        payload: dict[str, object] = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": dict(params),
        }
        await self._transport.request(payload)


class HttpMcpClient:
    """远程 HTTPS MCP Source 客户端。

    端点以 ``NormalizedEndpoint`` 入参强制“已通过 EndpointPolicy 校验”;
    传输注入,测试使用 fake transport,不发起真实网络请求。
    """

    def __init__(self, *, endpoint: NormalizedEndpoint, transport: McpJsonRpcTransport) -> None:
        self._endpoint = endpoint
        self._session = _JsonRpcSession(transport)

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        return await self._session.list_tools()

    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> McpToolCallResult:
        return await self._session.call_tool(name, arguments)


class StdioMcpClient:
    """本地 stdio MCP Source 客户端。

    命令行只能来自部署级 ``ServerKeyRegistry`` 的预注册条目(组合根先经
    ``McpToolSecurity`` 解析 Server Key,再把传输注入进来);本类不提供也
    不接受任何外部命令参数。stdio 帧传输注入,测试不启动子进程。
    """

    def __init__(self, *, server_key: str, transport: McpJsonRpcTransport) -> None:
        self._server_key = server_key
        self._session = _JsonRpcSession(transport)

    async def list_tools(self) -> tuple[McpToolDescriptor, ...]:
        return await self._session.list_tools()

    async def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> McpToolCallResult:
        return await self._session.call_tool(name, arguments)


class HttpxJsonRpcTransport:
    """基于 httpx 的 JSON-RPC 传输;网络异常归一为 ``McpTransportError``。"""

    def __init__(self, url: str, client: httpx.AsyncClient) -> None:
        self._url = url
        self._client = client

    async def request(self, payload: Mapping[str, object]) -> object:
        try:
            response = await self._client.post(self._url, json=dict(payload))
        except httpx.HTTPError as error:
            raise McpTransportError("MCP endpoint unreachable") from error
        if response.status_code >= 400:
            raise McpTransportError(
                f"MCP endpoint returned HTTP {response.status_code}"
            )
        if not response.content:
            return None
        return response.json()


__all__ = [
    "HttpMcpClient",
    "HttpxJsonRpcTransport",
    "McpClient",
    "McpJsonRpcTransport",
    "McpProtocolError",
    "McpToolCallResult",
    "McpToolDescriptor",
    "McpTransportError",
    "StdioMcpClient",
    "declared_effect_from_annotations",
    "descriptor_from_sdk_tool",
]
