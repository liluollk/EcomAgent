"""进程内能力 Registry 与注册记录(计划 02 Task 2;架构文档 §3、§4)。

- Registry 只按稳定 Key 与 Version ID 查找,不承担业务决策;
- 数据由装配方从数据库读出后注册,Registry 本身不查库;
- 重复注册与版本冲突在启动期(注册时)确定性拒绝,不以运行时猜测修复。

ToolBinding 只存在于 Worker 进程(架构文档 §3.3),连接数据定义与可执行
实现;Handler 侧其余字段(input_normalizer 等)由 PreToolUse 任务补充。
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from runtime.backend.protocol import AgentBackend
from runtime.capabilities.validation import (
    REGISTRY_DUPLICATE_KEY,
    REGISTRY_VERSION_CONFLICT,
    AssemblyError,
)
from runtime.interfaces.tools import ToolDefinition, ToolEffect


class VersionedRecord(BaseModel):
    """注册表记录公共字段:稳定 Key + 不可变 Version ID。"""

    model_config = ConfigDict(frozen=True)

    stable_key: str
    version_id: UUID
    version: int


class AgentDefinition(BaseModel):
    """AgentVersion ``definition_json`` 的组装视图(架构文档 §3.1)。

    只声明所需能力的稳定 Key;Workspace 绑定已发布版本,Run 创建时再把
    实际解析结果固定为精确 Version ID。definition_json 允许携带更多字段。
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str
    system_prompt_version: str | None = None
    required_backend_capabilities: frozenset[str]
    required_skill_keys: frozenset[str]
    required_tool_keys: frozenset[str]
    required_source_keys: frozenset[str]
    context_policy: dict[str, Any] = {}
    loop_limits: dict[str, Any] = {}


class SkillDefinition(BaseModel):
    """SkillVersion ``content_json`` 中影响组装的部分(架构文档 §3.4)。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str | None = None
    required_tool_keys: frozenset[str] = frozenset()
    required_source_keys: frozenset[str] = frozenset()
    prompt_fragments: dict[str, Any] = {}


class AgentVersionRecord(VersionedRecord):
    """已注册的 Agent 版本;``agent_id`` 指向 agents 行。"""

    agent_id: UUID
    definition: AgentDefinition


class SkillVersionRecord(VersionedRecord):
    """已注册的 Skill 版本;GLOBAL Skill 的 ``workspace_id`` 为空。"""

    workspace_id: UUID | None = None
    content: SkillDefinition = SkillDefinition()


class ToolVersionRecord(VersionedRecord):
    """已注册的 Tool 版本。

    ``effect`` 为 None 表示 MCP Tool 尚未经过本地 effect/permission 分类,
    未经分类的 MCP Tool 默认禁用;LOCAL Tool 的 effect 必须由定义给出。
    """

    workspace_id: UUID | None = None
    source_kind: Literal["LOCAL", "MCP"]
    effect: ToolEffect | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    schema_hash: str
    required_permissions: frozenset[str] = frozenset()
    source_version_id: UUID | None = None


class SourceVersionRecord(VersionedRecord):
    """已注册的 Source 版本;Source 属于 Workspace(架构文档 §3.5)。"""

    workspace_id: UUID
    kind: str


class ToolBinding(BaseModel):
    """Worker 进程内 Tool 与可执行实现的连接(架构文档 §3.3)。

    以 ``definition.stable_key`` 关联 Local Tool;写 Tool 必须通过
    ``write_adapter_capabilities`` 声明 Operation 与 Reconciliation 能力。
    """

    model_config = ConfigDict(frozen=True)

    definition: ToolDefinition
    handler: Any
    write_adapter_capabilities: frozenset[str] = frozenset()


class _VersionedRegistry[R: VersionedRecord]:
    """按稳定 Key 与 Version ID 的确定性查找;启动期拒绝注册冲突。"""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._by_key: dict[str, R] = {}
        self._by_version_id: dict[UUID, R] = {}

    def register(self, record: R) -> None:
        """注册一个版本;重复稳定 Key 或版本冲突立即拒绝。"""
        if record.stable_key in self._by_key:
            raise AssemblyError(
                REGISTRY_DUPLICATE_KEY,
                f"{self._kind} {record.stable_key} 重复注册",
            )
        if record.version_id in self._by_version_id:
            raise AssemblyError(
                REGISTRY_VERSION_CONFLICT,
                f"Version ID {record.version_id} 已被其他 {self._kind} 版本占用",
            )
        self._by_key[record.stable_key] = record
        self._by_version_id[record.version_id] = record

    def find_by_key(self, stable_key: str) -> R | None:
        return self._by_key.get(stable_key)

    def find_by_version_id(self, version_id: UUID) -> R | None:
        return self._by_version_id.get(version_id)

    def records(self) -> tuple[R, ...]:
        return tuple(self._by_key.values())


class AgentRegistry(_VersionedRegistry[AgentVersionRecord]):
    """AgentVersion 注册表;键为 ``agents.stable_key``。"""

    def __init__(self) -> None:
        super().__init__("Agent")


class SkillRegistry(_VersionedRegistry[SkillVersionRecord]):
    """SkillVersion 注册表;键为 ``skills.stable_key``。"""

    def __init__(self) -> None:
        super().__init__("Skill")


class SourceRegistry(_VersionedRegistry[SourceVersionRecord]):
    """SourceVersion 注册表;键为 ``sources.stable_key``。"""

    def __init__(self) -> None:
        super().__init__("Source")


class ToolRegistry(_VersionedRegistry[ToolVersionRecord]):
    """ToolVersion 注册表及其进程内 ToolBinding。"""

    def __init__(self) -> None:
        super().__init__("Tool")
        self._bindings: dict[str, list[ToolBinding]] = {}

    def bind(self, binding: ToolBinding) -> None:
        """登记一个 ToolBinding;同 Key 重复绑定由启动校验拒绝。"""
        self._bindings.setdefault(binding.definition.stable_key, []).append(binding)

    def bindings(self) -> tuple[ToolBinding, ...]:
        return tuple(
            binding for group in self._bindings.values() for binding in group
        )

    def bindings_for(self, stable_key: str) -> tuple[ToolBinding, ...]:
        return tuple(self._bindings.get(stable_key, ()))


class BackendRegistry:
    """AgentBackend 注册表;键为组合根选择的稳定 Backend Key。"""

    def __init__(self) -> None:
        self._backends: dict[str, AgentBackend] = {}

    def register(self, backend_key: str, backend: AgentBackend) -> None:
        if backend_key in self._backends:
            raise AssemblyError(
                REGISTRY_DUPLICATE_KEY,
                f"Backend {backend_key} 重复注册",
            )
        self._backends[backend_key] = backend

    def get(self, backend_key: str) -> AgentBackend | None:
        return self._backends.get(backend_key)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._backends)


__all__ = [
    "AgentDefinition",
    "AgentRegistry",
    "AgentVersionRecord",
    "BackendRegistry",
    "SkillDefinition",
    "SkillRegistry",
    "SkillVersionRecord",
    "SourceRegistry",
    "SourceVersionRecord",
    "ToolBinding",
    "ToolRegistry",
    "ToolVersionRecord",
    "VersionedRecord",
]
