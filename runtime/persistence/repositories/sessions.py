"""Session Repository:读写显式携带 workspace_id(架构文档 §5、§13)。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.models.interaction import Session


class SessionRepository:
    """sessions 表的持久化操作;只 flush,不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        created_by_actor_id: uuid.UUID,
        created_by_membership_id: uuid.UUID,
        title: str | None = None,
    ) -> Session:
        """创建 Session;``active_run_id`` 由创建/完成 Run 的事务维护。"""
        row = Session(
            workspace_id=workspace_id,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            created_by_actor_id=created_by_actor_id,
            created_by_membership_id=created_by_membership_id,
            title=title,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, workspace_id: uuid.UUID, session_id: uuid.UUID) -> Session | None:
        """按 (workspace_id, session_id) 读取;错误 workspace 返回 None。

        ``populate_existing`` 绕过身份映射,保证同事务内先写后读的一致性。
        """
        stmt = (
            select(Session)
            .where(Session.workspace_id == workspace_id, Session.id == session_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()
