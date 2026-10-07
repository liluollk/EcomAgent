"""Workspace 当前唯一有效 AgentVersion 的解析服务。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import ConfigVersionStatus
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    WorkspaceAgentBinding,
)


class WorkspaceAgentResolutionError(RuntimeError):
    """Workspace Agent 绑定无法唯一解析。"""


class WorkspaceAgentNotConfigured(WorkspaceAgentResolutionError):
    """Workspace 没有当前生效且已发布的 AgentVersion。"""


class WorkspaceAgentConfigurationInvalid(WorkspaceAgentResolutionError):
    """Workspace 同时存在多个有效 AgentVersion 绑定。"""


@dataclass(frozen=True)
class ResolvedWorkspaceAgent:
    """Session 创建所需的不可变 Agent 绑定摘要。"""

    agent_id: UUID
    agent_version_id: UUID
    stable_key: str
    name: str
    version: int


class WorkspaceAgentResolver:
    """按 Workspace 解析唯一有效且已发布的 AgentVersion。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, workspace_id: UUID) -> ResolvedWorkspaceAgent:
        rows = (
            await self._session.execute(
                sa.select(WorkspaceAgentBinding, AgentVersion, Agent)
                .join(
                    AgentVersion,
                    AgentVersion.id == WorkspaceAgentBinding.agent_version_id,
                )
                .join(Agent, Agent.id == AgentVersion.agent_id)
                .where(WorkspaceAgentBinding.workspace_id == workspace_id)
                .where(WorkspaceAgentBinding.enabled.is_(True))
                .where(WorkspaceAgentBinding.effective_at <= sa.func.now())
                .where(AgentVersion.status == ConfigVersionStatus.PUBLISHED)
                .order_by(WorkspaceAgentBinding.effective_at, WorkspaceAgentBinding.id)
            )
        ).all()
        if not rows:
            raise WorkspaceAgentNotConfigured(
                f"workspace {workspace_id} has no enabled agent version"
            )
        if len(rows) > 1:
            raise WorkspaceAgentConfigurationInvalid(
                f"workspace {workspace_id} has multiple enabled agent versions"
            )
        _, version, agent = rows[0]
        return ResolvedWorkspaceAgent(
            agent_id=agent.id,
            agent_version_id=version.id,
            stable_key=agent.stable_key,
            name=agent.name,
            version=version.version,
        )


__all__ = [
    "ResolvedWorkspaceAgent",
    "WorkspaceAgentConfigurationInvalid",
    "WorkspaceAgentNotConfigured",
    "WorkspaceAgentResolutionError",
    "WorkspaceAgentResolver",
]
