"""Run 级 Skill 按需加载与激活状态恢复。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.capabilities.validation import compute_schema_hash
from runtime.events.writer import AgentEventWriter
from runtime.interfaces.tools import ToolDefinition, ToolEffect
from runtime.orchestration.context import ActiveSkillInstruction
from runtime.persistence.models.configuration import Skill, SkillVersion
from runtime.persistence.models.events import AgentEvent
from runtime.persistence.models.execution import RunSkillVersion


LOAD_SKILL_KEY: Final = "load_skill"
SKILL_ACTIVATED: Final = "SKILL_ACTIVATED"

_LOAD_SKILL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"skill_key": {"type": "string", "minLength": 1}},
    "required": ["skill_key"],
    "additionalProperties": False,
}
_LOAD_SKILL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_key": {"type": "string"},
        "version": {"type": "integer"},
        "instructions": {"type": "string"},
        "already_active": {"type": "boolean"},
    },
    "required": ["skill_key", "version", "instructions", "already_active"],
    "additionalProperties": False,
}

LOAD_SKILL_DEFINITION: Final = ToolDefinition(
    stable_key=LOAD_SKILL_KEY,
    version=1,
    description="加载当前 Run 已固定的 Skill 完整指令。",
    input_json_schema=_LOAD_SKILL_INPUT_SCHEMA,
    output_json_schema=_LOAD_SKILL_OUTPUT_SCHEMA,
    effect=ToolEffect.READ,
    required_permissions=frozenset(),
    source_kind="LOCAL",
    schema_hash=compute_schema_hash(
        _LOAD_SKILL_INPUT_SCHEMA,
        _LOAD_SKILL_OUTPUT_SCHEMA,
    ),
)


@dataclass(frozen=True)
class RuntimeToolHandler:
    """ToolRegistry 中 Runtime 内置 Tool 的显式绑定标记。"""

    stable_key: str


LOAD_SKILL_HANDLER: Final = RuntimeToolHandler(stable_key=LOAD_SKILL_KEY)


class SkillActivationError(RuntimeError):
    """Skill 无法安全激活。"""


class SkillNotAvailable(SkillActivationError):
    """请求的 Skill 不属于当前 Run 的版本快照。"""


class SkillInstructionsInvalid(SkillActivationError):
    """SkillVersion 没有可加载的完整指令。"""


@dataclass(frozen=True)
class ActivatedSkill:
    """`load_skill` 的标准结果。"""

    skill_version_id: UUID
    stable_key: str
    version: int
    instructions: str
    already_active: bool

    def tool_payload(self) -> dict[str, Any]:
        return {
            "skill_key": self.stable_key,
            "version": self.version,
            "instructions": self.instructions,
            "already_active": self.already_active,
        }


class SkillActivationService:
    """激活 Run 已固定的 Skill，并从 AgentEvent 恢复激活状态。"""

    def __init__(self, session: AsyncSession, *, workspace_id: UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._writer = AgentEventWriter(session=session, workspace_id=workspace_id)

    async def activate(self, run_id: UUID, skill_key: str) -> ActivatedSkill:
        pinned = await self._pinned_skills(run_id)
        selected = next(
            (row for row in pinned if row[2].stable_key == skill_key),
            None,
        )
        if selected is None:
            raise SkillNotAvailable(
                f"skill {skill_key!r} is not pinned to run {run_id}"
            )
        _, version, skill = selected
        instructions = _instructions_of(version)
        events = await self._activation_events(run_id)
        already_active = any(
            str(event.payload.get("skill_version_id")) == str(version.id)
            for event in events
        )
        if not already_active:
            await self._writer.append(
                run_id,
                SKILL_ACTIVATED,
                {
                    "skill_id": str(skill.id),
                    "skill_version_id": str(version.id),
                    "skill_key": skill.stable_key,
                    "version": version.version,
                },
            )
        return ActivatedSkill(
            skill_version_id=version.id,
            stable_key=skill.stable_key,
            version=version.version,
            instructions=instructions,
            already_active=already_active,
        )

    async def active_for_run(self, run_id: UUID) -> tuple[ActiveSkillInstruction, ...]:
        pinned = await self._pinned_skills(run_id)
        by_version_id = {str(version.id): (version, skill) for _, version, skill in pinned}
        active: list[ActiveSkillInstruction] = []
        seen: set[str] = set()
        for event in await self._activation_events(run_id):
            version_id = str(event.payload.get("skill_version_id", ""))
            if version_id in seen or version_id not in by_version_id:
                continue
            version, skill = by_version_id[version_id]
            active.append(
                ActiveSkillInstruction(
                    stable_key=skill.stable_key,
                    version=version.version,
                    content=_instructions_of(version),
                )
            )
            seen.add(version_id)
        return tuple(active)

    async def _pinned_skills(
        self, run_id: UUID
    ) -> list[tuple[RunSkillVersion, SkillVersion, Skill]]:
        statement = (
            sa.select(RunSkillVersion, SkillVersion, Skill)
            .join(
                SkillVersion,
                SkillVersion.id == RunSkillVersion.skill_version_id,
            )
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(
                RunSkillVersion.workspace_id == self._workspace_id,
                RunSkillVersion.run_id == run_id,
            )
            .order_by(Skill.stable_key, SkillVersion.version)
        )
        return list((await self._session.execute(statement)).all())

    async def _activation_events(self, run_id: UUID) -> list[AgentEvent]:
        statement = (
            sa.select(AgentEvent)
            .where(
                AgentEvent.workspace_id == self._workspace_id,
                AgentEvent.run_id == run_id,
                AgentEvent.event_type == SKILL_ACTIVATED,
            )
            .order_by(AgentEvent.sequence_no)
        )
        return list((await self._session.scalars(statement)).all())


def _instructions_of(version: SkillVersion) -> str:
    prompt_fragments = version.content_json.get("prompt_fragments")
    instructions = (
        prompt_fragments.get("instructions")
        if isinstance(prompt_fragments, dict)
        else None
    )
    if not isinstance(instructions, str) or not instructions.strip():
        raise SkillInstructionsInvalid(
            f"skill version {version.id} has no instructions prompt fragment"
        )
    return instructions


__all__ = [
    "LOAD_SKILL_DEFINITION",
    "LOAD_SKILL_HANDLER",
    "LOAD_SKILL_KEY",
    "SKILL_ACTIVATED",
    "ActivatedSkill",
    "RuntimeToolHandler",
    "SkillActivationError",
    "SkillActivationService",
    "SkillInstructionsInvalid",
    "SkillNotAvailable",
]
