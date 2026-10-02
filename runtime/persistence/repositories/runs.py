"""Run Repository:状态推进只允许 CAS(架构文档 §5、§13)。"""

import uuid
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import RunStatus
from runtime.persistence.models.execution import Run


class RunRepository:
    """runs 表的持久化操作;只 flush 与条件更新,不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        created_by_actor_id: uuid.UUID,
        created_by_membership_id: uuid.UUID,
        agent_version_id: uuid.UUID,
        model_config_version_id: uuid.UUID,
        trigger_message_id: uuid.UUID | None = None,
        status: RunStatus = RunStatus.QUEUED,
        prompt_version: str | None = None,
        deadline_at: datetime | None = None,
    ) -> Run:
        """插入 Run 行;同一 Session 的活动 Run 唯一性由部分唯一索引兜底。"""
        run = Run(
            workspace_id=workspace_id,
            session_id=session_id,
            created_by_actor_id=created_by_actor_id,
            created_by_membership_id=created_by_membership_id,
            agent_version_id=agent_version_id,
            model_config_version_id=model_config_version_id,
            trigger_message_id=trigger_message_id,
            status=status,
            prompt_version=prompt_version,
            deadline_at=deadline_at,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> Run | None:
        """按 (workspace_id, run_id) 读取;错误 workspace 返回 None。

        ``populate_existing`` 绕过身份映射,避免 CAS 后读到过期的 ``state_version``。
        """
        stmt = (
            select(Run)
            .where(Run.workspace_id == workspace_id, Run.id == run_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def compare_and_set_status(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        expected_version: int,
        new_status: str,
        expected_status: str | None = None,
    ) -> bool:
        """CAS 状态推进(计划 01 Task 4、架构文档 §13)。

        UPDATE 的 WHERE 同时匹配 ``workspace_id``、``state_version = expected_version``,
        以及(提供 ``expected_status`` 时)当前状态;成功时 ``state_version`` 原子 +1。
        返回是否恰好更新到一行;零行表示版本过期、状态不匹配或越权。
        """
        conditions: list[sa.ColumnElement[bool]] = [
            Run.id == run_id,
            Run.workspace_id == workspace_id,
            Run.state_version == expected_version,
        ]
        if expected_status is not None:
            conditions.append(Run.status == expected_status)
        stmt = (
            sa.update(Run)
            .where(*conditions)
            .values(status=new_status, state_version=Run.state_version + 1)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return result.rowcount == 1
