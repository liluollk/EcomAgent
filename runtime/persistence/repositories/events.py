"""AgentEvent Repository:按 Run 追加事件与重放读取(架构文档 §10)。"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.models.events import AgentEvent


class EventRepository:
    """agent_events 表的持久化操作;只 flush,不提交事务。

    序号由调用方从 ``runs.next_event_sequence`` 分配(锁定或 CAS Run 原子取得),
    Repository 不使用 ``max(sequence_no) + 1``;``(run_id, sequence_no)`` 唯一性
    由 ``uq_event_sequence_per_run`` 兜底。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        sequence_no: int,
        event_type: str,
        payload: dict[str, Any],
        schema_version: int = 1,
        attempt_id: uuid.UUID | None = None,
        tool_call_id: uuid.UUID | None = None,
    ) -> AgentEvent:
        """追加一条 Run 事件;payload 必须已完成字段级脱敏。"""
        event = AgentEvent(
            workspace_id=workspace_id,
            session_id=session_id,
            run_id=run_id,
            sequence_no=sequence_no,
            event_type=event_type,
            payload=payload,
            schema_version=schema_version,
            attempt_id=attempt_id,
            tool_call_id=tool_call_id,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_run(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[AgentEvent]:
        """按 sequence_no 升序重放 Run 的全部事件((workspace_id, run_id) 索引)。"""
        stmt = (
            select(AgentEvent)
            .where(AgentEvent.workspace_id == workspace_id, AgentEvent.run_id == run_id)
            .order_by(AgentEvent.sequence_no)
            .execution_options(populate_existing=True)
        )
        rows = (await self._session.scalars(stmt)).all()
        return list(rows)
