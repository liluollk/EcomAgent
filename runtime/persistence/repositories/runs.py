"""Run Repository:状态推进只允许 CAS(架构文档 §5、§13)。"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import RunStatus
from runtime.persistence.models.execution import Run


@dataclass(frozen=True)
class EventSequenceAllocation:
    """一次原子事件序号分配的结果。"""

    sequence_no: int
    session_id: uuid.UUID


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

    async def allocate_event_sequence(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> EventSequenceAllocation | None:
        """CAS 式分配下一个事件序号(计划 02 Task 3;架构文档 §9)。

        单条 ``UPDATE runs SET next_event_sequence = next_event_sequence + 1
        RETURNING next_event_sequence - 1, session_id``:PostgreSQL 行锁使同一
        Run 的并发分配串行化,每个写入者取得互不相同的序号;序列分配与随后的
        AgentEvent INSERT 位于同一事务。返回 ``None`` 表示 (workspace_id, run_id)
        无匹配行(run 不存在或跨 workspace 越权)。
        """
        stmt = (
            sa.update(Run)
            .where(Run.workspace_id == workspace_id, Run.id == run_id)
            .values(next_event_sequence=Run.next_event_sequence + 1)
            .returning(
                (Run.next_event_sequence - 1).label("allocated_sequence_no"),
                Run.session_id,
            )
            .execution_options(synchronize_session=False)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return EventSequenceAllocation(
            sequence_no=int(row.allocated_sequence_no),
            session_id=cast("uuid.UUID", row.session_id),
        )
