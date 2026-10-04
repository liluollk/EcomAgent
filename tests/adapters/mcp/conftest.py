"""MCP adapter 测试夹具。

全部依赖注入:fake store(内存版 ToolVersion 端口)、fake client、
fake JSON-RPC transport,不发起真实网络请求,也不启动子进程。
EndpointPolicy 注入静态 resolver,不做真实 DNS 解析。
"""

from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

import pytest

from adapters.mcp.client import McpClient, McpToolCallResult, McpTransportError
from adapters.mcp.security import (
    McpToolSecurity,
    ServerKeyRegistry,
    StdioServerEntry,
)
from adapters.mcp.source_sync import McpSourceSync, McpToolVersionRecord
from adapters.mcp.tool_handler import McpToolHandler
from runtime.security.endpoints import EndpointPolicy


# 测试常量:端点 host 在部署 allowlist 中;resolver 返回公网 IP。
MCP_ENDPOINT_URL = "https://mcp.example.com/mcp"
WEKNORA_SERVER_KEY = "weknora-local"
WEKNORA_PREREGISTERED_COMMAND = ("/opt/weknora/weknora-mcp", "--stdio")


def _static_resolver(_hostname: str) -> Sequence[str]:
    return ["1.2.3.4"]


class FakeMcpClient:
    """可编程 fake:call_tool 返回预设结果或抛出预设异常,并记录调用。"""

    def __init__(
        self,
        *,
        call_result: McpToolCallResult | Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._call_result = (
            call_result if call_result is not None else McpToolCallResult(payload={"ok": True})
        )

    async def list_tools(self) -> tuple[object, ...]:
        return ()

    async def call_tool(self, name: str, arguments: Mapping[str, object]) -> McpToolCallResult:
        self.calls.append((name, dict(arguments)))
        if isinstance(self._call_result, Exception):
            raise self._call_result
        return self._call_result


class FakeJsonRpcTransport:
    """按脚本依次回放 JSON-RPC 响应;记录收到的请求报文。"""

    def __init__(self, results: Sequence[object]) -> None:
        self.requests: list[dict[str, object]] = []
        self._results = list(results)

    async def request(self, payload: Mapping[str, object]) -> object:
        self.requests.append(dict(payload))
        return self._results.pop(0)


class FakeMcpToolStore:
    """内存版 McpToolVersionStore;不做真实落库。"""

    def __init__(self) -> None:
        self._tools: dict[UUID, dict[str, object]] = {}
        self._versions: dict[UUID, McpToolVersionRecord] = {}
        self._versions_by_tool: dict[UUID, list[UUID]] = {}

    def get_or_create_tool(self, *, workspace_id: UUID, stable_key: str, name: str) -> UUID:
        for tool_id, tool in self._tools.items():
            if tool["workspace_id"] == workspace_id and tool["stable_key"] == stable_key:
                return tool_id
        tool_id = uuid4()
        self._tools[tool_id] = {
            "workspace_id": workspace_id,
            "stable_key": stable_key,
            "name": name,
        }
        return tool_id

    def latest_version(self, tool_id: UUID) -> McpToolVersionRecord | None:
        version_ids = self._versions_by_tool.get(tool_id, [])
        return self._versions[version_ids[-1]] if version_ids else None

    def create_version(self, record: McpToolVersionRecord) -> McpToolVersionRecord:
        self._versions[record.tool_version_id] = record
        self._versions_by_tool.setdefault(record.tool_id, []).append(record.tool_version_id)
        return record

    def get_version(self, tool_version_id: UUID) -> McpToolVersionRecord | None:
        return self._versions.get(tool_version_id)

    def save_version(self, record: McpToolVersionRecord) -> McpToolVersionRecord:
        self._versions[record.tool_version_id] = record
        return record


@pytest.fixture()
def workspace_id() -> UUID:
    return uuid4()


@pytest.fixture()
def tool_store() -> FakeMcpToolStore:
    return FakeMcpToolStore()


@pytest.fixture()
def source_sync(tool_store: FakeMcpToolStore, workspace_id: UUID) -> McpSourceSync:
    return McpSourceSync(store=tool_store, workspace_id=workspace_id)  # type: ignore[arg-type]


@pytest.fixture()
def endpoint_policy() -> EndpointPolicy:
    return EndpointPolicy(
        allowed_hosts={"mcp.example.com", "weknora.internal"},
        resolver=_static_resolver,
    )


@pytest.fixture()
def server_key_registry() -> ServerKeyRegistry:
    return ServerKeyRegistry(
        [
            StdioServerEntry(
                server_key=WEKNORA_SERVER_KEY, command=WEKNORA_PREREGISTERED_COMMAND
            )
        ]
    )


@pytest.fixture()
def security_factory(endpoint_policy: EndpointPolicy, server_key_registry: ServerKeyRegistry):
    """构建注入 fake client 工厂的 McpToolSecurity。"""

    def build(
        http_client: FakeMcpClient | None = None,
        stdio_client_factory=None,
    ) -> McpToolSecurity:
        resolved = http_client if http_client is not None else FakeMcpClient()

        def default_stdio_factory(_entry: StdioServerEntry) -> McpClient:
            return FakeMcpClient()

        return McpToolSecurity(
            endpoint_policy=endpoint_policy,
            server_keys=server_key_registry,
            http_client_factory=lambda _endpoint: resolved,
            stdio_client_factory=(
                stdio_client_factory
                if stdio_client_factory is not None
                else default_stdio_factory
            ),
        )

    return build


@pytest.fixture()
def mcp_tool_handler(security_factory) -> McpToolHandler:
    """计划用例夹具:WeKnora Source 不可用(连接失败)。"""
    unavailable = FakeMcpClient(call_result=McpTransportError("weknora outage"))
    return McpToolHandler(security=security_factory(http_client=unavailable))


@pytest.fixture()
def make_fake_client():
    """返回 FakeMcpClient 类,供测试按用例配置调用结果/异常。"""
    return FakeMcpClient


@pytest.fixture()
def make_fake_transport():
    """返回 FakeJsonRpcTransport 类,供 JSON-RPC 帧测试注入脚本响应。"""
    return FakeJsonRpcTransport
