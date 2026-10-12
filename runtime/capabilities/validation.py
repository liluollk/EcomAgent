"""启动组装校验与 AssemblyError(计划 02 Task 2;架构文档 §4)。

校验内容:
- 每个启用的 AgentVersion 都能找到所需 Skill、Tool 和 Source 版本;
- 每个 Local ToolVersion 恰好有一个 ToolBinding;
- ToolDefinition 的 ``schema_hash`` 与注册 Schema 一致;
- 每个写 Tool 都声明 Operation 与 Reconciliation 能力(无法回查时显式
  声明 ``reconciliation.unsupported``);
- 当前 AgentBackend 满足 Agent 必需能力;
- MCP Tool 未经本地 effect/permission 分类不得启用;
- 重复注册或版本冲突在注册时(启动期)确定性拒绝。

本模块只对注册表做结构校验,类型仅用于注解(TYPE_CHECKING 导入),
避免与 ``registry`` 形成运行时循环依赖。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from runtime.execution.pretool.decisions import TOOL_PRETOOL_PROFILE_MISSING


if TYPE_CHECKING:  # pragma: no cover - 仅类型检查需要
    from runtime.backend.protocol import AgentBackend
    from runtime.capabilities.registry import (
        AgentRegistry,
        AgentVersionRecord,
        BackendRegistry,
        SkillRegistry,
        SourceRegistry,
        ToolBinding,
        ToolRegistry,
        ToolVersionRecord,
    )

# ---- 注册表原因码 ----
REGISTRY_DUPLICATE_KEY: Final = "REGISTRY_DUPLICATE_KEY"
REGISTRY_VERSION_CONFLICT: Final = "REGISTRY_VERSION_CONFLICT"

# ---- 组装校验原因码 ----
AGENT_SKILL_VERSION_MISSING: Final = "AGENT_SKILL_VERSION_MISSING"
AGENT_TOOL_VERSION_MISSING: Final = "AGENT_TOOL_VERSION_MISSING"
AGENT_SOURCE_VERSION_MISSING: Final = "AGENT_SOURCE_VERSION_MISSING"
AGENT_VERSION_NOT_FOUND: Final = "AGENT_VERSION_NOT_FOUND"
BACKEND_CAPABILITY_INSUFFICIENT: Final = "BACKEND_CAPABILITY_INSUFFICIENT"
SCHEMA_HASH_MISMATCH: Final = "SCHEMA_HASH_MISMATCH"
MCP_TOOL_UNCLASSIFIED: Final = "MCP_TOOL_UNCLASSIFIED"
MISSING_TOOL_BINDING: Final = "MISSING_TOOL_BINDING"
DUPLICATE_TOOL_BINDING: Final = "DUPLICATE_TOOL_BINDING"
UNKNOWN_TOOL_BINDING: Final = "UNKNOWN_TOOL_BINDING"
WRITE_TOOL_CAPABILITY_MISSING: Final = "WRITE_TOOL_CAPABILITY_MISSING"

# ---- PreToolProfile / 执行 Profile 校验原因码(架构文档 §3.3、§9)----
WRITE_TOOL_POLICY_MISSING: Final = "WRITE_TOOL_POLICY_MISSING"
WRITE_TOOL_RESOURCE_RESOLVER_MISSING: Final = "WRITE_TOOL_RESOURCE_RESOLVER_MISSING"
WRITE_TOOL_EXECUTION_PROFILE_MISSING: Final = "WRITE_TOOL_EXECUTION_PROFILE_MISSING"
READ_TOOL_EXECUTION_PROFILE_FORBIDDEN: Final = "READ_TOOL_EXECUTION_PROFILE_FORBIDDEN"

# 写 Adapter 能力声明(架构文档 §3.3)。
OPERATION_CAPABILITY: Final = "operation"
RECONCILIATION_CAPABILITY: Final = "reconciliation"
RECONCILIATION_UNSUPPORTED: Final = "reconciliation.unsupported"


class AssemblyError(Exception):
    """启动组装失败;``reason_code`` 为确定性原因码,消息含原因码。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code


def compute_schema_hash(
    input_schema: Mapping[str, object],
    output_schema: Mapping[str, object],
) -> str:
    """按规范化 JSON 计算 Tool Schema 哈希(SHA-256)。"""
    canonical = json.dumps(
        {"input": input_schema, "output": output_schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_assembly(
    *,
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    sources: SourceRegistry,
    backends: BackendRegistry,
    backend_key: str | None = None,
) -> None:
    """执行全部启动校验;任一失败即抛出携带 reason_code 的 AssemblyError。"""
    _validate_agent_requirements(agents, skills, tools, sources, backends, backend_key)
    _validate_tool_versions(tools.records())
    _validate_tool_bindings(tools)


def _validate_agent_requirements(
    agents: AgentRegistry,
    skills: SkillRegistry,
    tools: ToolRegistry,
    sources: SourceRegistry,
    backends: BackendRegistry,
    backend_key: str | None,
) -> None:
    for agent in agents.records():
        for key in sorted(agent.definition.required_skill_keys):
            if skills.find_by_key(key) is None:
                raise AssemblyError(
                    AGENT_SKILL_VERSION_MISSING,
                    f"Agent {agent.stable_key} 找不到所需 Skill 版本 {key}",
                )
        for key in sorted(agent.definition.required_tool_keys):
            if tools.find_by_key(key) is None:
                raise AssemblyError(
                    AGENT_TOOL_VERSION_MISSING,
                    f"Agent {agent.stable_key} 找不到所需 Tool 版本 {key}",
                )
        for key in sorted(agent.definition.required_source_keys):
            if sources.find_by_key(key) is None:
                raise AssemblyError(
                    AGENT_SOURCE_VERSION_MISSING,
                    f"Agent {agent.stable_key} 找不到所需 Source 版本 {key}",
                )
        _validate_backend_capabilities(agent, backends, backend_key)


def _validate_backend_capabilities(
    agent: AgentVersionRecord,
    backends: BackendRegistry,
    backend_key: str | None,
) -> None:
    required = agent.definition.required_backend_capabilities
    if not required:
        return
    backend: AgentBackend | None
    if backend_key is not None:
        backend = backends.get(backend_key)
    else:
        keys = backends.keys()
        backend = backends.get(keys[0]) if len(keys) == 1 else None
    if backend is None:
        raise AssemblyError(
            BACKEND_CAPABILITY_INSUFFICIENT,
            f"Agent {agent.stable_key} 无法确定 Backend:"
            f" 注册了 {len(backends.keys())} 个 Backend 且未指定 backend_key",
        )
    missing = sorted(required - backend.capabilities().capability_keys)
    if missing:
        raise AssemblyError(
            BACKEND_CAPABILITY_INSUFFICIENT,
            f"Agent {agent.stable_key} 的 Backend 缺少能力: {', '.join(missing)}",
        )


def _validate_tool_versions(records: tuple[ToolVersionRecord, ...]) -> None:
    for record in records:
        expected = compute_schema_hash(record.input_schema, record.output_schema)
        if record.schema_hash != expected:
            raise AssemblyError(
                SCHEMA_HASH_MISMATCH,
                f"Tool {record.stable_key} v{record.version} 的 schema_hash"
                " 与注册 Schema 不一致",
            )
        if record.source_kind == "MCP" and record.effect is None:
            raise AssemblyError(
                MCP_TOOL_UNCLASSIFIED,
                f"MCP Tool {record.stable_key} 未经本地 effect/permission 分类,默认禁用",
            )


def _validate_tool_bindings(tools: ToolRegistry) -> None:
    for binding in tools.bindings():
        if tools.find_by_key(binding.definition.stable_key) is None:
            raise AssemblyError(
                UNKNOWN_TOOL_BINDING,
                f"ToolBinding 引用了未注册的 Tool {binding.definition.stable_key}",
            )
    for record in tools.records():
        if record.source_kind != "LOCAL":
            continue
        bindings = tools.bindings_for(record.stable_key)
        if not bindings:
            raise AssemblyError(
                MISSING_TOOL_BINDING,
                f"Local Tool {record.stable_key} v{record.version} 没有 ToolBinding",
            )
        if len(bindings) > 1:
            raise AssemblyError(
                DUPLICATE_TOOL_BINDING,
                f"Local Tool {record.stable_key} 绑定了 {len(bindings)} 个 ToolBinding,"
                " 应恰好一个",
            )
        _validate_write_capabilities(record, bindings[0])
        validate_tool_binding(bindings[0])


def validate_tool_binding(binding: ToolBinding) -> None:
    """校验单个 ToolBinding 的 PreToolProfile 与执行 Profile(启动期 fail-closed)。

    规则(架构文档 §3.3、§9):

    - 每个 Tool 必须显式注册 PreToolProfile(不得依赖 Pipeline 的隐式默认);
    - 写 Tool 必须有业务策略(``BUSINESS``)、资源解析器(``RESOURCE``)与
      执行 Profile;缺失分别以稳定错误码拒绝;
    - 只读 Tool 不得绑定执行 Profile(写执行元数据只属于写 Tool)。
    """
    key = binding.definition.stable_key
    registration = binding.pretool_registration
    if registration is None:  # 防御:字段必填,正常路径不会是 None
        raise AssemblyError(
            TOOL_PRETOOL_PROFILE_MISSING,
            f"Tool {key} 未注册 PreToolProfile",
        )
    is_write = binding.definition.effect == "WRITE"
    if is_write:
        if registration.policy_mode != "BUSINESS":
            raise AssemblyError(
                WRITE_TOOL_POLICY_MISSING,
                f"写 Tool {key} 缺少 business_policy(BUSINESS 模式)",
            )
        if registration.resource_mode != "RESOURCE":
            raise AssemblyError(
                WRITE_TOOL_RESOURCE_RESOLVER_MISSING,
                f"写 Tool {key} 缺少 resource_resolver(RESOURCE 模式)",
            )
        if binding.execution_profile is None:
            raise AssemblyError(
                WRITE_TOOL_EXECUTION_PROFILE_MISSING,
                f"写 Tool {key} 缺少 execution_profile",
            )
    elif binding.execution_profile is not None:
        raise AssemblyError(
            READ_TOOL_EXECUTION_PROFILE_FORBIDDEN,
            f"只读 Tool {key} 不允许绑定 execution_profile",
        )


def _validate_write_capabilities(
    record: ToolVersionRecord,
    binding: ToolBinding,
) -> None:
    if record.effect != "WRITE":
        return
    declared = binding.write_adapter_capabilities
    missing: list[str] = []
    if OPERATION_CAPABILITY not in declared:
        missing.append(OPERATION_CAPABILITY)
    if (
        RECONCILIATION_CAPABILITY not in declared
        and RECONCILIATION_UNSUPPORTED not in declared
    ):
        missing.append(RECONCILIATION_CAPABILITY)
    if missing:
        raise AssemblyError(
            WRITE_TOOL_CAPABILITY_MISSING,
            f"写 Tool {record.stable_key} 未声明能力: {', '.join(missing)}",
        )


__all__ = [
    "AGENT_SKILL_VERSION_MISSING",
    "AGENT_SOURCE_VERSION_MISSING",
    "AGENT_TOOL_VERSION_MISSING",
    "AGENT_VERSION_NOT_FOUND",
    "BACKEND_CAPABILITY_INSUFFICIENT",
    "DUPLICATE_TOOL_BINDING",
    "MCP_TOOL_UNCLASSIFIED",
    "MISSING_TOOL_BINDING",
    "OPERATION_CAPABILITY",
    "READ_TOOL_EXECUTION_PROFILE_FORBIDDEN",
    "RECONCILIATION_CAPABILITY",
    "RECONCILIATION_UNSUPPORTED",
    "REGISTRY_DUPLICATE_KEY",
    "REGISTRY_VERSION_CONFLICT",
    "SCHEMA_HASH_MISMATCH",
    "TOOL_PRETOOL_PROFILE_MISSING",
    "UNKNOWN_TOOL_BINDING",
    "WRITE_TOOL_CAPABILITY_MISSING",
    "WRITE_TOOL_EXECUTION_PROFILE_MISSING",
    "WRITE_TOOL_POLICY_MISSING",
    "WRITE_TOOL_RESOURCE_RESOLVER_MISSING",
    "AssemblyError",
    "compute_schema_hash",
    "validate_assembly",
    "validate_tool_binding",
]
