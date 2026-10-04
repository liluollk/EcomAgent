"""MCP Tool 调用编排(计划 04 Task 6;架构文档 §3.3、§6)。

失败/超时确定性分类:
- ``SOURCE_UNAVAILABLE``:必需知识源(如 WeKnora)不可用或超时;
- ``DEGRADED``:可选 Source 不可用,返回降级结果(不带数据);
- ``TOOL_ERROR``:MCP 工具自身确定性失败(isError 结果或协议错误);
- ``EndpointRejected`` 不被吞掉,作为确定性拒绝向上传播。

WeKnora 是独立部署的检索服务,本仓库只保存连接配置并把它作为普通
MCP Source 接入;检索结果只进入 Tool Message(不可信数据),不复制
其知识库能力。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from adapters.mcp.client import McpProtocolError, McpTransportError
from adapters.mcp.security import McpSourceRef, McpToolSecurity, UntrustedToolData


INVOKE_OK: Final = "OK"
SOURCE_UNAVAILABLE: Final = "SOURCE_UNAVAILABLE"
DEGRADED: Final = "DEGRADED"
TOOL_ERROR: Final = "TOOL_ERROR"

REASON_MCP_SOURCE_UNAVAILABLE: Final = "MCP_SOURCE_UNAVAILABLE"
REASON_MCP_SOURCE_TIMEOUT: Final = "MCP_SOURCE_TIMEOUT"
REASON_MCP_PROTOCOL_ERROR: Final = "MCP_PROTOCOL_ERROR"
REASON_MCP_TOOL_ERROR: Final = "MCP_TOOL_ERROR"

_DEFAULT_TIMEOUT_SECONDS: Final = 30.0


class McpToolCallContext(BaseModel):
    """一次 MCP 工具调用的上下文。"""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    arguments: Mapping[str, Any] = Field(default_factory=dict)
    source_ref: McpSourceRef
    knowledge_required: bool = False


class McpToolInvokeResult(BaseModel):
    """调用回执:``code`` 为确定性分类,``data`` 仅在成功时存在且不可信。"""

    model_config = ConfigDict(frozen=True)

    code: str
    data: UntrustedToolData | None = None
    reason_code: str | None = None


class McpToolHandler:
    """经 McpClient 调用 MCP 工具并按确定性语义分类结果。

    client 由 ``McpToolSecurity`` 解析(测试注入 fake client);本类不发起
    真实网络请求,也不启动子进程。
    """

    def __init__(
        self, *, security: McpToolSecurity, timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self._security = security
        self._timeout_seconds = timeout_seconds

    async def invoke(self, context: McpToolCallContext) -> McpToolInvokeResult:
        # EndpointRejected(端点校验失败)在此确定性传播,不降级、不重试。
        client = self._security.resolve_client(context.source_ref)
        try:
            call = await asyncio.wait_for(
                client.call_tool(context.tool_name, dict(context.arguments)),
                self._timeout_seconds,
            )
        except TimeoutError:
            return self._source_outcome(context, REASON_MCP_SOURCE_TIMEOUT)
        except McpTransportError:
            return self._source_outcome(context, REASON_MCP_SOURCE_UNAVAILABLE)
        except McpProtocolError:
            return McpToolInvokeResult(
                code=TOOL_ERROR, reason_code=REASON_MCP_PROTOCOL_ERROR
            )
        if call.is_error:
            return McpToolInvokeResult(code=TOOL_ERROR, reason_code=REASON_MCP_TOOL_ERROR)
        data = self._security.wrap_untrusted_data(context.source_ref, call.payload)
        return McpToolInvokeResult(code=INVOKE_OK, data=data)

    @staticmethod
    def _source_outcome(
        context: McpToolCallContext, reason_code: str
    ) -> McpToolInvokeResult:
        """源不可用/超时:必需知识源按失败处理,可选源按降级处理。"""
        if context.knowledge_required:
            return McpToolInvokeResult(code=SOURCE_UNAVAILABLE, reason_code=reason_code)
        return McpToolInvokeResult(code=DEGRADED, reason_code=reason_code)


__all__ = [
    "DEGRADED",
    "INVOKE_OK",
    "SOURCE_UNAVAILABLE",
    "TOOL_ERROR",
    "McpToolCallContext",
    "McpToolHandler",
    "McpToolInvokeResult",
]
