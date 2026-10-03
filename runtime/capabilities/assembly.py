"""RuntimeAssembly:聚合注册表并执行启动校验(计划 02 Task 2;架构文档 §4)。

Worker 组合根显式构造本对象;``validate()`` 执行架构文档 §4 的启动校验,
``resolve()`` 给定 Agent 稳定 Key 解析出钉住的精确 Version ID 集合。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from runtime.capabilities.registry import (
    AgentRegistry,
    AgentVersionRecord,
    BackendRegistry,
    SkillRegistry,
    SkillVersionRecord,
    SourceRegistry,
    SourceVersionRecord,
    ToolRegistry,
    ToolVersionRecord,
)
from runtime.capabilities.validation import (
    AGENT_SKILL_VERSION_MISSING,
    AGENT_SOURCE_VERSION_MISSING,
    AGENT_TOOL_VERSION_MISSING,
    AGENT_VERSION_NOT_FOUND,
    AssemblyError,
    validate_assembly,
)


class ResolvedVersions(BaseModel):
    """给定 Agent 稳定 Key 解析出的钉住版本集合。

    恢复执行时使用这些精确 Version ID;权限、Execution Mode、Business
    Policy 与资源状态仍在写前读当前值。
    """

    model_config = ConfigDict(frozen=True)

    agent: AgentVersionRecord
    skills: tuple[SkillVersionRecord, ...]
    tools: tuple[ToolVersionRecord, ...]
    sources: tuple[SourceVersionRecord, ...]

    @property
    def tool_version_ids(self) -> frozenset[UUID]:
        return frozenset(record.version_id for record in self.tools)

    @property
    def skill_version_ids(self) -> frozenset[UUID]:
        return frozenset(record.version_id for record in self.skills)

    @property
    def source_version_ids(self) -> frozenset[UUID]:
        return frozenset(record.version_id for record in self.sources)


class RuntimeAssembly:
    """组合根显式构造的运行时组装(架构文档 §4;后续任务补充其余组件)。"""

    def __init__(
        self,
        *,
        agents: AgentRegistry,
        skills: SkillRegistry,
        tools: ToolRegistry,
        sources: SourceRegistry,
        backends: BackendRegistry,
        backend_key: str | None = None,
    ) -> None:
        self.agents = agents
        self.skills = skills
        self.tools = tools
        self.sources = sources
        self.backends = backends
        self.backend_key = backend_key

    def validate(self) -> None:
        """执行启动校验;任一失败即抛出 AssemblyError(带 reason_code)。"""
        validate_assembly(
            agents=self.agents,
            skills=self.skills,
            tools=self.tools,
            sources=self.sources,
            backends=self.backends,
            backend_key=self.backend_key,
        )

    def resolve(self, agent_key: str) -> ResolvedVersions:
        """按稳定 Key 解析 Agent 及其所需 Skill/Tool/Source 的精确版本。"""
        agent = self.agents.find_by_key(agent_key)
        if agent is None:
            raise AssemblyError(AGENT_VERSION_NOT_FOUND, f"Agent {agent_key} 未注册")

        skill_records: list[SkillVersionRecord] = []
        for key in sorted(agent.definition.required_skill_keys):
            skill_record = self.skills.find_by_key(key)
            if skill_record is None:
                raise AssemblyError(
                    AGENT_SKILL_VERSION_MISSING,
                    f"Agent {agent_key} 找不到所需 Skill 版本 {key}",
                )
            skill_records.append(skill_record)

        tool_records: list[ToolVersionRecord] = []
        for key in sorted(agent.definition.required_tool_keys):
            tool_record = self.tools.find_by_key(key)
            if tool_record is None:
                raise AssemblyError(
                    AGENT_TOOL_VERSION_MISSING,
                    f"Agent {agent_key} 找不到所需 Tool 版本 {key}",
                )
            tool_records.append(tool_record)

        source_records: list[SourceVersionRecord] = []
        for key in sorted(agent.definition.required_source_keys):
            source_record = self.sources.find_by_key(key)
            if source_record is None:
                raise AssemblyError(
                    AGENT_SOURCE_VERSION_MISSING,
                    f"Agent {agent_key} 找不到所需 Source 版本 {key}",
                )
            source_records.append(source_record)

        return ResolvedVersions(
            agent=agent,
            skills=tuple(skill_records),
            tools=tuple(tool_records),
            sources=tuple(source_records),
        )


__all__ = [
    "ResolvedVersions",
    "RuntimeAssembly",
]
