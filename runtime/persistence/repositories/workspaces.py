"""Workspace Repository:全部操作以 workspace 主键为边界(架构文档 §13)。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import ExecutionMode
from runtime.persistence.models.identity import Workspace


class WorkspaceRepository:
    """workspaces 表的持久化操作;只 flush,不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        name: str,
        execution_mode: ExecutionMode = ExecutionMode.READ_ONLY,
    ) -> Workspace:
        """创建 Workspace;主键与 state_version 由应用侧默认值生成。"""
        workspace = Workspace(name=name, execution_mode=execution_mode)
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    async def get(self, workspace_id: uuid.UUID) -> Workspace | None:
        """按主键读取 Workspace。"""
        stmt = (
            select(Workspace)
            .where(Workspace.id == workspace_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()
