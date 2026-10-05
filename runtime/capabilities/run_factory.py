"""Run 创建服务:钉住解析版本并写入快照关联表(计划 02 Task 2)。

Run 创建时把实际解析的 Agent/Skill/Tool/Source 版本固定为精确 Version ID,
写入 ``runs`` 与 ``run_skill_versions`` / ``run_tool_versions`` /
``run_source_versions``;恢复使用这些版本,但权限、Execution Mode、
Business Policy 与资源状态仍在写前读当前值(见 ``write_service``)。

本服务在调用方事务内执行(与 ``ActorContextBuilder`` 相同约定),不自行
提交;组合根可在 UnitOfWork 上下文中传入其 Session。

注:当前 41 表 Schema 的 ``runs`` 没有权限快照列(禁止新增 migration),
创建时的授权视图随 ``PinnedRun.permission_snapshot`` 返回,仅作审计解释;
执行授权永远读当前库态,不依赖该快照。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.access.actor import ActorContextBuilder
from runtime.capabilities.assembly import ResolvedVersions, RuntimeAssembly
from runtime.persistence.models.execution import Run
from runtime.persistence.repositories.run_snapshots import RunSnapshotRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


class PinnedRun(BaseModel):
    """新建 Run 及其钉住的版本集合与创建时授权视图。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: Run
    resolved: ResolvedVersions
    permission_snapshot: dict[str, Any]

    @property
    def id(self) -> UUID:
        return self.run.id

    @property
    def workspace_id(self) -> UUID:
        return self.run.workspace_id

    @property
    def actor_id(self) -> UUID:
        return self.run.created_by_actor_id

    @property
    def membership_id(self) -> UUID:
        return self.run.created_by_membership_id

    @property
    def agent_version_id(self) -> UUID:
        return self.resolved.agent.version_id

    @property
    def tool_version_ids(self) -> frozenset[UUID]:
        return self.resolved.tool_version_ids

    @property
    def skill_version_ids(self) -> frozenset[UUID]:
        return self.resolved.skill_version_ids

    @property
    def source_version_ids(self) -> frozenset[UUID]:
        return self.resolved.source_version_ids


class RunFactory:
    """在调用方事务内创建 Run:先解析版本,再落库并写快照关联表。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        assembly: RuntimeAssembly,
        workspace_id: UUID,
        actor_id: UUID,
        membership_id: UUID,
        model_config_version_id: UUID,
    ) -> None:
        self._session = session
        self._assembly = assembly
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._membership_id = membership_id
        self._model_config_version_id = model_config_version_id

    async def create(self, *, agent_key: str) -> PinnedRun:
        """解析并钉住 ``agent_key`` 的版本集合,创建 Session 与 Run。"""
        resolved = self._assembly.resolve(agent_key)
        # 创建时的授权视图仅作审计;不作为执行期的授权依据。
        actor = await ActorContextBuilder(self._session).build(
            actor_id=self._actor_id,
            workspace_id=self._workspace_id,
            request_id=f"run-{uuid4()}",
        )
        permission_snapshot = {
            "membership_id": str(actor.membership_id),
            "role": actor.role.value,
            "permissions": sorted(actor.permissions),
        }

        session_row = await SessionRepository(self._session).create(
            workspace_id=self._workspace_id,
            agent_id=resolved.agent.agent_id,
            agent_version_id=resolved.agent.version_id,
            created_by_actor_id=self._actor_id,
            created_by_membership_id=self._membership_id,
        )
        run = await RunRepository(self._session).create(
            workspace_id=self._workspace_id,
            session_id=session_row.id,
            created_by_actor_id=self._actor_id,
            created_by_membership_id=self._membership_id,
            agent_version_id=resolved.agent.version_id,
            model_config_version_id=self._model_config_version_id,
        )
        await RunSnapshotRepository(self._session).replace(
            run_id=run.id,
            workspace_id=self._workspace_id,
            tool_version_ids=resolved.tool_version_ids,
            skill_version_ids=resolved.skill_version_ids,
            source_version_ids=resolved.source_version_ids,
        )
        return PinnedRun(
            run=run,
            resolved=resolved,
            permission_snapshot=permission_snapshot,
        )


__all__ = ["PinnedRun", "RunFactory"]
