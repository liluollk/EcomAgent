"""Run 版本快照关联表仓库(计划 02 Task 2;数据模型文档 §5)。

覆盖 ``run_tool_versions`` / ``run_skill_versions`` / ``run_source_versions``,
固定当次运行实际使用的精确版本;与其它 Repository 一样只执行 flush 与
条件语句,绝不调用 commit —— 事务边界由调用方拥有。
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.models.execution import (
    RunSkillVersion,
    RunSourceVersion,
    RunToolVersion,
)


class RunSnapshotRepository:
    """Run 版本快照的幂等写入与读取。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace(
        self,
        *,
        run_id: UUID,
        workspace_id: UUID,
        tool_version_ids: Iterable[UUID] = (),
        skill_version_ids: Iterable[UUID] = (),
        source_version_ids: Iterable[UUID] = (),
    ) -> None:
        """以给定集合整体替换 Run 的版本快照(创建与恢复重放均幂等)。

        ``workspace_id`` 为 Run 所属 Workspace:Skill/Tool 快照可引用
        GLOBAL 版本(单列外键),Source 快照使用 (workspace_id, id) 复合外键。
        """
        for model in (RunToolVersion, RunSkillVersion, RunSourceVersion):
            await self._session.execute(
                sa.delete(model).where(
                    model.run_id == run_id,
                    model.workspace_id == workspace_id,
                )
            )
        self._session.add_all(
            RunToolVersion(run_id=run_id, workspace_id=workspace_id, tool_version_id=version_id)
            for version_id in tool_version_ids
        )
        self._session.add_all(
            RunSkillVersion(run_id=run_id, workspace_id=workspace_id, skill_version_id=version_id)
            for version_id in skill_version_ids
        )
        self._session.add_all(
            RunSourceVersion(
                run_id=run_id, workspace_id=workspace_id, source_version_id=version_id
            )
            for version_id in source_version_ids
        )
        await self._session.flush()

    async def tool_version_ids(self, run_id: UUID) -> list[UUID]:
        stmt = select(RunToolVersion.tool_version_id).where(RunToolVersion.run_id == run_id)
        return list(await self._session.scalars(stmt))

    async def skill_version_ids(self, run_id: UUID) -> list[UUID]:
        stmt = select(RunSkillVersion.skill_version_id).where(RunSkillVersion.run_id == run_id)
        return list(await self._session.scalars(stmt))

    async def source_version_ids(self, run_id: UUID) -> list[UUID]:
        stmt = select(RunSourceVersion.source_version_id).where(RunSourceVersion.run_id == run_id)
        return list(await self._session.scalars(stmt))


__all__ = ["RunSnapshotRepository"]
