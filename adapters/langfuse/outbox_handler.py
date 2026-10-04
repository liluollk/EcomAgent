"""Langfuse Outbox Handler(计划 04 Task 8)。

只处理 ``TRACE_EXPORT``:现读该 Run 的 AgentEvent(按 sequence_no 升序),
经 :class:`TraceExporter` 字段级脱敏后调注入的 :class:`LangfuseClient`
提交。集成失败只改变 Outbox 状态(经 OutboxWorker 预算重试或置 DEAD),
绝不读取也不修改 Run 状态。失败归一:
- 客户端任意异常 → :class:`LangfuseUnavailableError`(可重试);
- aggregate 非 Run id / Run 无事件可导出 → :class:`LangfuseInvalidTraceError`
  (不可重试)。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.langfuse.client import (
    LangfuseClient,
    LangfuseExportError,
    LangfuseInvalidTraceError,
    LangfuseUnavailableError,
)
from adapters.langfuse.exporter import TraceEventSnapshot, TraceExporter
from runtime.integrations.outbox_worker import OutboxEntry, OutboxError
from runtime.persistence.base import OutboxKind
from runtime.persistence.models.events import AgentEvent


class LangfuseOutboxHandler:
    """把 TRACE_EXPORT Outbox 行导出为 Langfuse Trace;调用方事务内只 flush。"""

    def __init__(
        self,
        session: AsyncSession,
        client: LangfuseClient,
        *,
        exporter: TraceExporter | None = None,
    ) -> None:
        self._session = session
        self._exporter = exporter if exporter is not None else TraceExporter(client)

    async def handle(self, entry: OutboxEntry) -> None:
        if entry.kind is not OutboxKind.TRACE_EXPORT:
            raise OutboxError(
                "UNSUPPORTED_OUTBOX_KIND",
                f"langfuse handler cannot process outbox kind {entry.kind.value}",
            )
        run_id = self._parse_run_id(entry)
        events = await self._load_events(entry, run_id)
        if not events:
            raise LangfuseInvalidTraceError(
                "TRACE_RUN_MISSING",
                f"run {run_id} has no events to export for outbox {entry.outbox_id}",
            )
        try:
            await self._exporter.export_events(
                [self._snapshot(event) for event in events]
            )
        except LangfuseExportError:
            raise
        except Exception as error:
            raise LangfuseUnavailableError(
                "LANGFUSE_UNAVAILABLE", f"langfuse export for run {run_id} failed"
            ) from error

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_run_id(entry: OutboxEntry) -> uuid.UUID:
        try:
            return uuid.UUID(entry.aggregate_id)
        except ValueError as error:
            raise LangfuseInvalidTraceError(
                "TRACE_AGGREGATE_INVALID",
                f"outbox {entry.outbox_id} aggregate {entry.aggregate_id} is not a run id",
            ) from error

    async def _load_events(
        self, entry: OutboxEntry, run_id: uuid.UUID
    ) -> list[AgentEvent]:
        stmt = (
            select(AgentEvent)
            .where(
                AgentEvent.workspace_id == entry.workspace_id,
                AgentEvent.run_id == run_id,
            )
            .order_by(AgentEvent.sequence_no)
        )
        return list((await self._session.scalars(stmt)).all())

    @staticmethod
    def _snapshot(event: AgentEvent) -> TraceEventSnapshot:
        """ORM 行 → 只读快照;ID 全部字符串化,避免 ORM 对象进入映射。"""
        return TraceEventSnapshot(
            event_type=event.event_type,
            run_id=str(event.run_id),
            session_id=str(event.session_id),
            sequence_no=event.sequence_no,
            payload=dict(event.payload),
            event_id=str(event.id),
            attempt_id=str(event.attempt_id) if event.attempt_id is not None else None,
            tool_call_id=str(event.tool_call_id) if event.tool_call_id is not None else None,
            workspace_id=str(event.workspace_id),
            occurred_at=event.created_at,
        )


__all__ = ["LangfuseOutboxHandler"]
