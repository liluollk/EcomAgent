"""MCP 工具安全边界(计划 04 Task 6;架构文档 §6、§9)。

- ``McpSourceRef``:来源引用,``extra="forbid"``——请求/前端携带的 command
  字段直接被拒绝;HTTP 必须给端点 URL,STDIO 必须给部署级 Server Key;
- ``EndpointPolicy`` 包装:HTTP Source 端点必须通过 SSRF/allowlist 校验,
  ``EndpointRejected`` 向上确定性传播(不重试、不降级);
- ``ServerKeyRegistry``:部署期一次性预注册 stdio Server Key → 命令,运行期
  只读;未知 Key 确定性拒绝;命令不接受来自前端或请求的输入;
- ``wrap_untrusted_data``:MCP 调用结果一律包装为不可信数据,只能回填
  TOOL Message,不得升级为 System Message(架构文档 §6)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from adapters.mcp.client import McpClient
from runtime.security.endpoints import EndpointPolicy, NormalizedEndpoint


MCP_SERVER_KEY_UNKNOWN: Final = "MCP_SERVER_KEY_UNKNOWN"
MCP_SOURCE_REF_INVALID: Final = "MCP_SOURCE_REF_INVALID"


class McpSecurityError(Exception):
    """MCP 来源解析被确定性拒绝;``reason_code`` 说明原因。"""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


class McpSourceRef(BaseModel):
    """MCP Source 引用。

    ``extra="forbid"`` 使前端/请求携带的 ``command`` 等字段在模型校验阶段
    即被拒绝;HTTP 走端点 URL,STDIO 走部署级 Server Key,二者互斥。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_kind: Literal["HTTP", "STDIO"]
    endpoint_url: str | None = None
    server_key: str | None = None

    @model_validator(mode="after")
    def _enforce_shape(self) -> McpSourceRef:
        if self.source_kind == "HTTP":
            if not self.endpoint_url:
                raise ValueError("HTTP source requires endpoint_url")
            if self.server_key is not None:
                raise ValueError("HTTP source must not carry server_key")
        else:
            if not self.server_key:
                raise ValueError("STDIO source requires a deployed server_key")
            if self.endpoint_url is not None:
                raise ValueError("STDIO source must not carry endpoint_url")
        return self

    def describe(self) -> str:
        """脱敏的来源标识;URL 已在上游完成规范化与校验。"""
        return self.endpoint_url if self.endpoint_url is not None else f"stdio:{self.server_key}"


class StdioServerEntry(BaseModel):
    """部署级预注册的 stdio Server 条目;``command`` 只能来自部署配置。"""

    model_config = ConfigDict(frozen=True)

    server_key: str
    command: tuple[str, ...]


class ServerKeyRegistry:
    """部署级 stdio Server Key 注册表:构造时一次性预注册,运行期只读。"""

    def __init__(self, entries: Sequence[StdioServerEntry]) -> None:
        self._entries = {entry.server_key: entry for entry in entries}

    def resolve(self, server_key: str) -> StdioServerEntry:
        entry = self._entries.get(server_key)
        if entry is None:
            raise McpSecurityError(
                MCP_SERVER_KEY_UNKNOWN,
                f"stdio server key {server_key!r} is not preregistered at deployment",
            )
        return entry


class UntrustedToolData(BaseModel):
    """MCP 调用结果的不可信包装。

    只允许作为 TOOL Message 回填后续 Context;不得升级为 System Message,
    也不得修改 Runtime 规则(架构文档 §6)。
    """

    model_config = ConfigDict(frozen=True)

    trust_level: Literal["UNTRUSTED"] = "UNTRUSTED"
    allowed_context_role: Literal["TOOL"] = "TOOL"
    source_kind: Literal["MCP"] = "MCP"
    source_ref: str
    payload: object


class HttpClientFactory(Protocol):
    def __call__(self, endpoint: NormalizedEndpoint) -> McpClient: ...


class StdioClientFactory(Protocol):
    def __call__(self, entry: StdioServerEntry) -> McpClient: ...


class McpToolSecurity:
    """MCP Source 访问的安全包装:端点校验、Server Key 解析与结果包装。

    client 工厂由组合根注入(生产连 HttpMcpClient/StdioMcpClient,测试注入
    fake client);本类自身不做网络与子进程操作。
    """

    def __init__(
        self,
        *,
        endpoint_policy: EndpointPolicy,
        server_keys: ServerKeyRegistry,
        http_client_factory: HttpClientFactory,
        stdio_client_factory: StdioClientFactory,
    ) -> None:
        self._endpoint_policy = endpoint_policy
        self._server_keys = server_keys
        self._http_client_factory = http_client_factory
        self._stdio_client_factory = stdio_client_factory

    def validate_source_url(self, url: str) -> NormalizedEndpoint:
        """EndpointPolicy 包装;``EndpointRejected`` 确定性传播。"""
        return self._endpoint_policy.validate_model_endpoint(url)

    def resolve_client(self, source_ref: McpSourceRef) -> McpClient:
        """把来源引用解析为可调用客户端;任何不确定因素在此被拒绝。"""
        if source_ref.source_kind == "HTTP":
            url = source_ref.endpoint_url
            if url is None:
                raise McpSecurityError(
                    MCP_SOURCE_REF_INVALID, "HTTP source requires endpoint_url"
                )
            return self._http_client_factory(self.validate_source_url(url))
        server_key = source_ref.server_key
        if server_key is None:
            raise McpSecurityError(
                MCP_SOURCE_REF_INVALID, "STDIO source requires a deployed server_key"
            )
        entry = self._server_keys.resolve(server_key)
        return self._stdio_client_factory(entry)

    def wrap_untrusted_data(self, source_ref: McpSourceRef, payload: object) -> UntrustedToolData:
        """把 MCP 调用结果标记为不可信数据,绑定来源与允许的上下文角色。"""
        return UntrustedToolData(source_ref=source_ref.describe(), payload=payload)


__all__ = [
    "MCP_SERVER_KEY_UNKNOWN",
    "MCP_SOURCE_REF_INVALID",
    "HttpClientFactory",
    "McpSecurityError",
    "McpSourceRef",
    "McpToolSecurity",
    "ServerKeyRegistry",
    "StdioClientFactory",
    "StdioServerEntry",
    "UntrustedToolData",
]
