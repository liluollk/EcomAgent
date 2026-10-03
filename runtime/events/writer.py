"""AgentEventWriter:Run 事件追加器,序号在事务内原子分配(计划 02 Task 3;架构文档 §9)。

序号分配策略:``runs`` 表已含 ``next_event_sequence`` 列(41 表 Schema,
server default 1),故采用 **CAS 式自增**——经
:class:`~runtime.persistence.repositories.runs.RunRepository.allocate_event_sequence`
执行单条 ``UPDATE runs SET next_event_sequence = next_event_sequence + 1
WHERE workspace_id = :ws AND id = :run RETURNING next_event_sequence - 1,
session_id``。PostgreSQL 行锁使同一 Run 的并发分配天然串行化,每个写入者
取得互不相同的序号;序列分配与 AgentEvent INSERT 位于同一事务,无需
``max(sequence_no) + 1`` 扫描,也无需 INSERT 后依赖唯一索引重试
(``uq_event_sequence_per_run`` 仅作兜底)。

每个 AgentEvent 至少携带 ``event_id``、``run_id`` 与 Run 内单调递增的
``sequence_no``;本写入器只 flush,不 commit——状态变更与 AgentEvent 在
同一 PostgreSQL 事务内提交的边界由调用方(UnitOfWork)持有。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.repositories.events import EventRepository
from runtime.persistence.repositories.runs import RunRepository


class RunNotFoundError(LookupError):
    """(workspace_id, run_id) 无匹配 Run 行(不存在或跨 workspace 越权)。"""


@dataclass(frozen=True)
class AppendedEvent:
    """``append()`` 返回的只读事件视图(不暴露 ORM 对象)。"""

    event_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID
    sequence_no: int
    event_type: str
    payload: dict[str, Any]
    schema_version: int
    attempt_id: uuid.UUID | None
    tool_call_id: uuid.UUID | None


class AgentEventWriter:
    """绑定 AsyncSession 与 workspace 上下文的 Run 事件追加器。

    ``append(run_id, event_kind, payload)`` 自动分配 Run 内单调递增的
    ``sequence_no``(策略见模块 docstring),并在同一事务内 INSERT 事件;
    只 flush,不 commit。

    单个 AsyncSession 不允许多个任务并发执行语句(asyncpg 单连接下
    ``asyncio.gather`` 会产生 flush 交叠),故 ``append`` 以实例级
    ``asyncio.Lock`` 串行化:同一 writer 上的并发调用顺序执行,跨连接的
    真实并发仍由 ``allocate_event_sequence`` 的行锁保证序号唯一。
    """

    def __init__(self, *, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._runs = RunRepository(session)
        self._events = EventRepository(session)
        self._append_lock = asyncio.Lock()

    @property
    def workspace_id(self) -> uuid.UUID:
        """写入器绑定的 workspace。"""
        return self._workspace_id

    async def append(
        self,
        run_id: uuid.UUID,
        event_kind: str,
        payload: dict[str, Any],
        *,
        schema_version: int = 1,
        attempt_id: uuid.UUID | None = None,
        tool_call_id: uuid.UUID | None = None,
    ) -> AppendedEvent:
        """在调用方事务内分配序号并追加一条 Run 事件;只 flush 不 commit。

        ``payload`` 必须已完成字段级脱敏(持久化层不再校验)。
        """
        # 单一 AsyncSession 不可被多任务并发使用;实例级锁将并发 append
        # 串行化(见类 docstring),序号唯一性在跨连接场景由行锁保证。
        async with self._append_lock:
            allocation = await self._runs.allocate_event_sequence(
                workspace_id=self._workspace_id, run_id=run_id
            )
            if allocation is None:
                raise RunNotFoundError(
                    f"run {run_id} not found in workspace {self._workspace_id}"
                )
            event = await self._events.append(
                workspace_id=self._workspace_id,
                session_id=allocation.session_id,
                run_id=run_id,
                sequence_no=allocation.sequence_no,
                event_type=event_kind,
                payload=payload,
                schema_version=schema_version,
                attempt_id=attempt_id,
                tool_call_id=tool_call_id,
            )
            return AppendedEvent(
                event_id=event.id,
                workspace_id=event.workspace_id,
                session_id=event.session_id,
                run_id=event.run_id,
                sequence_no=event.sequence_no,
                event_type=event.event_type,
                payload=event.payload,
                schema_version=event.schema_version,
                attempt_id=event.attempt_id,
                tool_call_id=event.tool_call_id,
            )
