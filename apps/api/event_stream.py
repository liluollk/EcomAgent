"""AgentEvent 流式重放服务(计划 04 Task 4;架构文档《Runtime执行状态与恢复设计》§事件交付)。

- 重放:cursor(事件 UUID id 或整数序号)之后的已提交事件按 ``sequence_no``
  升序全部推送;
- 轮询:首批重放后按 ``poll_interval`` 轮询新提交事件;事件只在状态事务提交后
  可见——每次读取使用独立短事务,不在连接上持有长事务;
- 交付语义:at-least-once。同一连接内服务端以 cursor 推进不重复推送;断线后
  客户端按最后处理的 event id 重连重放可能重复,客户端按 ``id`` 去重、按
  ``sequence_no`` 检测缺口(缺口时由客户端重新获取包含 cursor 的快照);
- 序列化:``{id, sequence_no, event_kind, payload, created_at}``。payload 为
  入库时已完成字段级脱敏的内容,流层逐字透传,不追加任何字段。
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.errors import NOT_FOUND, VALIDATION_ERROR, ApiError
from runtime.persistence.models.events import AgentEvent


DEFAULT_POLL_INTERVAL_SECONDS: Final = 0.5
"""轮询已提交新事件的默认周期;可经构造参数注入覆盖。"""


@dataclass(frozen=True)
class EventMessage:
    """推送给客户端的单条事件视图。"""

    id: uuid.UUID
    sequence_no: int
    event_kind: str
    payload: dict[str, Any]
    created_at: datetime

    def to_json(self) -> dict[str, Any]:
        """序列化为对外消息体;字段集合固定,不携带内部列。"""
        return {
            "id": str(self.id),
            "sequence_no": self.sequence_no,
            "event_kind": self.event_kind,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


def _to_message(event: AgentEvent) -> EventMessage:
    return EventMessage(
        id=event.id,
        sequence_no=int(event.sequence_no),
        event_kind=event.event_type,
        payload=dict(event.payload),
        created_at=event.created_at,
    )


class EventStreamService:
    """按 Run 重放并轮询推送已提交 AgentEvent;不依赖进程内状态,可多连接并发。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须为正数")
        self._sessionmaker = sessionmaker
        self._poll_interval = poll_interval

    async def resolve_start_sequence(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        cursor: str | None,
    ) -> int:
        """解析 cursor 为排他起始序号(重放 ``sequence_no > 起始序号``)。

        - 缺省/空:从 Run 头重放(序号 0 之后);
        - 整数:直接作为起始序号(与 Run 快照 ``events_cursor`` 对齐);
        - UUID:先解析该事件在本 Run 内的 ``sequence_no``;格式非法 422,
          事件不存在或属于其它 Run 404。
        """
        if cursor is None or cursor.strip() == "":
            return 0
        text = cursor.strip()
        if text.isdigit():
            return int(text)
        try:
            cursor_id = uuid.UUID(text)
        except ValueError as error:
            raise ApiError(
                status_code=422, code=VALIDATION_ERROR, message="cursor 必须是事件 id 或序号"
            ) from error
        async with self._sessionmaker() as session:
            sequence_no = await session.scalar(
                select(AgentEvent.sequence_no).where(
                    AgentEvent.workspace_id == workspace_id,
                    AgentEvent.run_id == run_id,
                    AgentEvent.id == cursor_id,
                )
            )
        if sequence_no is None:
            raise ApiError(status_code=404, code=NOT_FOUND, message="cursor 事件不存在")
        return int(sequence_no)

    async def stream(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        after_sequence: int,
    ) -> AsyncGenerator[EventMessage, None]:
        """先重放 ``after_sequence`` 之后的全部已提交事件,再轮询推送新事件。

        每批读取都在独立短事务中完成(会话随批关闭);一批为空时等待
        ``poll_interval`` 后再查。生成器无限,由调用方在连接断开时取消。
        """
        cursor_sequence = after_sequence
        while True:
            batch = await self._committed_events(
                workspace_id=workspace_id, run_id=run_id, after_sequence=cursor_sequence
            )
            for event in batch:
                cursor_sequence = int(event.sequence_no)
                yield _to_message(event)
            if not batch:
                await asyncio.sleep(self._poll_interval)

    async def _committed_events(
        self,
        *,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        after_sequence: int,
    ) -> list[AgentEvent]:
        """读取一段已提交事件,按 sequence_no 升序;独立短事务,只读不写。"""
        async with self._sessionmaker() as session:
            rows = (
                await session.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.workspace_id == workspace_id,
                        AgentEvent.run_id == run_id,
                        AgentEvent.sequence_no > after_sequence,
                    )
                    .order_by(AgentEvent.sequence_no)
                    .execution_options(populate_existing=True)
                )
            ).all()
            return list(rows)
