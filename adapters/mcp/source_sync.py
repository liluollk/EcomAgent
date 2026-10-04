"""MCP Source 同步:Schema 版本化与本地分类门禁(计划 04 Task 6 Step 3)。

语义(架构文档 §3.2、§4):
- 远程 Schema 保存为新的 Workspace ToolVersion(``source_kind="MCP"``);
- MCP Server 声明的 effect/permission 一律忽略:新版本初始 ``enabled=False``、
  ``disabled_reason=LOCAL_CLASSIFICATION_REQUIRED``,未经本地分类不得启用;
- Schema 或描述变化时创建新版本,不覆盖正在运行的旧版本;
- 同一 Schema 且描述未变时重复同步幂等(返回同一 tool_version_id,已分类
  版本的启用状态保持不变);
- 本地受信配置经 ``classify`` 绑定 effect 与 required_permissions 后才启用。

持久化经 ``McpToolVersionStore`` 端口注入(数据库实现由组合根装配;测试
使用内存 fake)。schema_hash 复用
``runtime.capabilities.validation.compute_schema_hash`` 的规范化 JSON SHA-256。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from adapters.mcp.client import McpToolDescriptor
from runtime.capabilities.validation import compute_schema_hash
from runtime.interfaces.tools import ToolEffect


LOCAL_CLASSIFICATION_REQUIRED: Final = "LOCAL_CLASSIFICATION_REQUIRED"


class McpToolVersionRecord(BaseModel):
    """同步产物:MCP Tool 的不可变版本记录及分类/启用状态。"""

    model_config = ConfigDict(frozen=True)

    tool_version_id: UUID
    tool_id: UUID
    workspace_id: UUID
    stable_key: str
    name: str
    description: str
    version: int
    source_kind: Literal["MCP"] = "MCP"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    schema_hash: str
    effect: ToolEffect | None = None
    required_permissions: frozenset[str] = frozenset()
    enabled: bool = False
    disabled_reason: str | None = LOCAL_CLASSIFICATION_REQUIRED


class McpToolVersionStore(Protocol):
    """MCP Tool 版本持久化端口;实现方负责 tool/tool_version 行的落库。"""

    def get_or_create_tool(
        self, *, workspace_id: UUID, stable_key: str, name: str
    ) -> UUID: ...

    def latest_version(self, tool_id: UUID) -> McpToolVersionRecord | None: ...

    def create_version(
        self, record: McpToolVersionRecord
    ) -> McpToolVersionRecord: ...

    def get_version(self, tool_version_id: UUID) -> McpToolVersionRecord | None: ...

    def save_version(
        self, record: McpToolVersionRecord
    ) -> McpToolVersionRecord: ...


class McpToolVersionNotFoundError(Exception):
    """classify 指定的 ToolVersion 不存在。"""


class McpSourceSync:
    """把 MCP Server 工具声明同步为本地 ToolVersion,并执行分类门禁。"""

    def __init__(
        self,
        *,
        store: McpToolVersionStore,
        workspace_id: UUID,
        tool_key_prefix: str = "mcp",
    ) -> None:
        self._store = store
        self._workspace_id = workspace_id
        self._tool_key_prefix = tool_key_prefix

    async def sync(self, tool: McpToolDescriptor) -> McpToolVersionRecord:
        """同步一个 MCP Server 工具声明;其 effect/permission 声明被忽略。

        Schema(哈希)或描述未变时幂等返回现有版本;变化时新建下一版本
        (未分类、禁用),旧版本保持不变。
        """
        stable_key = f"{self._tool_key_prefix}.{tool.name}"
        tool_id = self._store.get_or_create_tool(
            workspace_id=self._workspace_id, stable_key=stable_key, name=tool.name
        )
        schema_hash = compute_schema_hash(tool.input_schema, tool.output_schema)
        latest = self._store.latest_version(tool_id)
        if (
            latest is not None
            and latest.schema_hash == schema_hash
            and latest.description == tool.description
        ):
            return latest
        record = McpToolVersionRecord(
            tool_version_id=uuid4(),
            tool_id=tool_id,
            workspace_id=self._workspace_id,
            stable_key=stable_key,
            name=tool.name,
            description=tool.description,
            version=1 if latest is None else latest.version + 1,
            input_schema=dict(tool.input_schema),
            output_schema=dict(tool.output_schema),
            schema_hash=schema_hash,
        )
        return self._store.create_version(record)

    async def classify(
        self,
        tool_version_id: UUID,
        *,
        effect: ToolEffect,
        required_permissions: Iterable[str] = (),
    ) -> McpToolVersionRecord:
        """本地受信分类绑定:绑定 effect/required_permissions 后启用工具。

        只有部署组合根或 WorkspaceOwner 的受信配置可以调用;MCP Server
        声明不能进入本入口。
        """
        record = self._store.get_version(tool_version_id)
        if record is None:
            raise McpToolVersionNotFoundError(
                f"tool version {tool_version_id} not found"
            )
        updated = record.model_copy(
            update={
                "effect": effect,
                "required_permissions": frozenset(required_permissions),
                "enabled": True,
                "disabled_reason": None,
            }
        )
        return self._store.save_version(updated)


__all__ = [
    "LOCAL_CLASSIFICATION_REQUIRED",
    "McpSourceSync",
    "McpToolVersionNotFoundError",
    "McpToolVersionRecord",
    "McpToolVersionStore",
]
