"""TRACE_EXPORT Outbox 挂接点(计划 04 Task 8)。

最小实现:``enqueue_trace_export`` 在当前事务内追加一条 PENDING
``TRACE_EXPORT`` Outbox 行,供 Run/AgentEvent 写入路径后续接线调用。
Outbox 行不内嵌事件内容——``payload_ref`` 指向 Run,导出时由 handler
现读 AgentEvent 再脱敏,避免 Secret 经 Outbox 二次落库;本任务测试直接用
``enqueue + OutboxWorker`` 驱动全链路,不改 Runtime 各写入点(接线策略:
事件追加器/Run 状态迁移成功后调用本函数,与业务同事务提交)。
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import OutboxKind
from runtime.persistence.models.integrations import IntegrationOutbox


TRACE_EXPORT_AGGREGATE_TYPE: Final = "run"
"""TRACE_EXPORT 行的 aggregate_type:指向权威聚合 Run。"""


async def enqueue_trace_export(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    payload_ref: str | None = None,
) -> IntegrationOutbox:
    """追加一条 PENDING TRACE_EXPORT Outbox 行;只 add+flush,不 commit。"""
    row = IntegrationOutbox(
        workspace_id=workspace_id,
        kind=OutboxKind.TRACE_EXPORT,
        aggregate_type=TRACE_EXPORT_AGGREGATE_TYPE,
        aggregate_id=str(run_id),
        payload_ref=payload_ref if payload_ref is not None else f"run:{run_id}",
    )
    session.add(row)
    await session.flush()
    return row


__all__ = ["TRACE_EXPORT_AGGREGATE_TYPE", "enqueue_trace_export"]
