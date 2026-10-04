"""MCP 工具安全与调用编排测试(计划 04 Task 6 Step 1)。

覆盖:WeKnora 停机按知识源必需性降级/失败、MCP 结果包装为不可信数据
(只进 TOOL Message)、远程端点经 EndpointPolicy 确定性拒绝、stdio 只经
部署级 Server Key 选择、handler 调用注入的 fake client(无真实网络/子进程)。
"""

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from adapters.mcp.client import (
    HttpMcpClient,
    McpProtocolError,
    McpToolCallResult,
    McpToolDescriptor,
)
from adapters.mcp.security import (
    MCP_SERVER_KEY_UNKNOWN,
    McpSecurityError,
    McpSourceRef,
    StdioServerEntry,
)
from adapters.mcp.tool_handler import McpToolCallContext, McpToolHandler
from runtime.security.endpoints import (
    REASON_ADDRESS_FORBIDDEN,
    REASON_SCHEME_FORBIDDEN,
    EndpointRejected,
    NormalizedEndpoint,
)


# 测试常量:HTTP 端点 host 在部署 allowlist 中;stdio 命令为部署预注册条目。
MCP_ENDPOINT_URL = "https://mcp.example.com/mcp"
WEKNORA_PREREGISTERED_COMMAND = ("/opt/weknora/weknora-mcp", "--stdio")


def unavailable_weknora_tool(*, required: bool) -> McpToolCallContext:
    return McpToolCallContext(
        tool_name="weknora_search",
        arguments={"query": "退货政策"},
        source_ref=McpSourceRef(source_kind="HTTP", endpoint_url=MCP_ENDPOINT_URL),
        knowledge_required=required,
    )


def _tool_call_context(source_ref: McpSourceRef) -> McpToolCallContext:
    return McpToolCallContext(
        tool_name="weknora_search",
        arguments={"query": "退货政策"},
        source_ref=source_ref,
    )


@pytest.mark.parametrize(
    ("knowledge_required", "expected"),
    [(False, "DEGRADED"), (True, "SOURCE_UNAVAILABLE")],
)
async def test_weknora_outage_respects_source_requirement(
    mcp_tool_handler, knowledge_required, expected
) -> None:
    result = await mcp_tool_handler.invoke(
        unavailable_weknora_tool(required=knowledge_required)
    )
    assert result.code == expected


async def test_timeout_is_classified_like_source_outage(
    security_factory, make_fake_client
) -> None:
    handler = McpToolHandler(
        security=security_factory(http_client=make_fake_client(call_result=TimeoutError()))
    )
    degraded = await handler.invoke(unavailable_weknora_tool(required=False))
    assert degraded.code == "DEGRADED"
    assert degraded.reason_code == "MCP_SOURCE_TIMEOUT"

    unavailable = await handler.invoke(unavailable_weknora_tool(required=True))
    assert unavailable.code == "SOURCE_UNAVAILABLE"


async def test_deterministic_errors_are_not_source_outages(
    security_factory, make_fake_client
) -> None:
    protocol_error = McpToolHandler(
        security=security_factory(
            http_client=make_fake_client(call_result=McpProtocolError("bad request"))
        )
    )
    result = await protocol_error.invoke(unavailable_weknora_tool(required=True))
    assert result.code == "TOOL_ERROR"
    assert result.reason_code == "MCP_PROTOCOL_ERROR"

    tool_error = McpToolHandler(
        security=security_factory(
            http_client=make_fake_client(
                call_result=McpToolCallResult(payload={"error": "boom"}, is_error=True)
            )
        )
    )
    failed = await tool_error.invoke(unavailable_weknora_tool(required=False))
    assert failed.code == "TOOL_ERROR"
    assert failed.reason_code == "MCP_TOOL_ERROR"
    assert failed.data is None


async def test_mcp_result_is_wrapped_as_untrusted_tool_data(
    security_factory, make_fake_client
) -> None:
    client = make_fake_client(call_result=McpToolCallResult(payload={"rows": ["row-1"]}))
    handler = McpToolHandler(security=security_factory(http_client=client))

    result = await handler.invoke(unavailable_weknora_tool(required=True))

    assert result.code == "OK"
    assert result.data is not None
    assert result.data.trust_level == "UNTRUSTED"
    assert result.data.allowed_context_role == "TOOL"
    assert result.data.source_kind == "MCP"
    assert result.data.source_ref == MCP_ENDPOINT_URL
    assert result.data.payload == {"rows": ["row-1"]}
    # 不可信数据只能回填 TOOL Message,不得升级为 System Message。
    assert result.data.allowed_context_role != "SYSTEM"
    assert client.calls == [("weknora_search", {"query": "退货政策"})]


async def test_unsafe_endpoint_url_is_rejected_deterministically(
    security_factory,
) -> None:
    handler = McpToolHandler(security=security_factory())
    context = _tool_call_context(
        McpSourceRef(source_kind="HTTP", endpoint_url="http://mcp.example.com/mcp")
    )
    with pytest.raises(EndpointRejected) as scheme:
        await handler.invoke(context)
    assert scheme.value.reason_code == REASON_SCHEME_FORBIDDEN

    security = security_factory()
    with pytest.raises(EndpointRejected) as address:
        security.validate_source_url("https://169.254.169.254/latest")
    assert address.value.reason_code == REASON_ADDRESS_FORBIDDEN


def test_stdio_source_requires_deployed_server_key(security_factory) -> None:
    security = security_factory()
    with pytest.raises(McpSecurityError) as unknown_key:
        security.resolve_client(
            McpSourceRef(source_kind="STDIO", server_key="not-registered")
        )
    assert unknown_key.value.reason_code == MCP_SERVER_KEY_UNKNOWN


def test_stdio_source_rejects_command_from_request(security_factory) -> None:
    # McpSourceRef extra="forbid":前端/请求携带的 command 字段直接被拒绝。
    with pytest.raises(ValidationError):
        McpSourceRef.model_validate(
            {
                "source_kind": "STDIO",
                "server_key": "weknora-local",
                "command": ["powershell.exe", "-c", "evil"],
            }
        )

    captured: list[StdioServerEntry] = []

    def stdio_factory(entry: StdioServerEntry) -> object:
        captured.append(entry)
        return object()

    security = security_factory(stdio_client_factory=stdio_factory)
    client = security.resolve_client(
        McpSourceRef(source_kind="STDIO", server_key="weknora-local")
    )
    assert client is not None
    # stdio 命令只来自部署级预注册条目(配置注入),不接受外部命令。
    assert captured[0].command == WEKNORA_PREREGISTERED_COMMAND


async def test_http_client_speaks_jsonrpc_over_injected_transport(
    make_fake_transport,
) -> None:
    transport = make_fake_transport(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "weknora", "version": "1"},
                },
            },
            None,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {
                    "tools": [
                        {
                            "name": "weknora_search",
                            "description": "检索 WeKnora 知识库",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"query": {"type": "string"}},
                            },
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "content": [{"type": "text", "text": "row-1"}],
                    "isError": False,
                },
            },
        ]
    )
    endpoint = NormalizedEndpoint(
        url="https://mcp.example.com/mcp",
        scheme="https",
        host="mcp.example.com",
        port=443,
    )
    client = HttpMcpClient(endpoint=endpoint, transport=transport)

    tools: tuple[McpToolDescriptor, ...] = await client.list_tools()
    assert [tool.name for tool in tools] == ["weknora_search"]
    # 服务器声明只透传为 declared_effect,采纳与否由本地分类决定。
    assert tools[0].declared_effect == "READ"

    call = await client.call_tool("weknora_search", {"query": "退货"})
    assert call.is_error is False
    assert call.payload == "row-1"

    methods = [str(request["method"]) for request in transport.requests]
    assert methods[0] == "initialize"
    assert methods[1] == "notifications/initialized"
    assert methods[2] == "tools/list"
    assert methods[3] == "tools/call"

    call_request: Mapping[str, Any] = transport.requests[3]
    assert call_request["params"] == {
        "name": "weknora_search",
        "arguments": {"query": "退货"},
    }
