"""Mem0 Outbox Handler(计划 04 Task 7 Step 3)。

按 Outbox kind 调用注入的 :class:`Mem0Client`:
- ``MEMORY_UPSERT`` → ``add``(携带本地 kind/subject_key/revision 元数据,
  成功后把本地记录 ``sync_status`` 推进为 SYNCED);
- ``MEMORY_DELETE`` → ``delete``(本地 Tombstone 保持不变)。

任何客户端失败归一为 :class:`Mem0SyncError`(:class:`OutboxError` 子类),
由 OutboxWorker 统一按预算重试或置 DEAD;绝不修改 Run 状态。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from adapters.mem0.client import Mem0Client, Mem0SyncError
from runtime.integrations.outbox_worker import OutboxEntry, OutboxError
from runtime.persistence.base import MemorySyncStatus, OutboxKind
from runtime.persistence.models.integrations import MemoryRecord


def mem0_user_id(workspace_id: uuid.UUID, actor_id: uuid.UUID) -> str:
    """Mem0 user_id:按 ``workspace:actor`` 隔离外部记忆命名空间。"""
    return f"{workspace_id}:{actor_id}"


class Mem0OutboxHandler:
    """把记忆权威变更同步到 Mem0;在调用方事务内执行,只 flush。"""

    def __init__(self, session: AsyncSession, client: Mem0Client) -> None:
        self._session = session
        self._client = client

    async def handle(self, entry: OutboxEntry) -> None:
        if entry.kind is OutboxKind.MEMORY_UPSERT:
            await self._upsert(entry)
        elif entry.kind is OutboxKind.MEMORY_DELETE:
            await self._delete(entry)
        else:
            raise OutboxError(
                "UNSUPPORTED_OUTBOX_KIND",
                f"mem0 handler cannot process outbox kind {entry.kind.value}",
            )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _upsert(self, entry: OutboxEntry) -> None:
        record = await self._load(entry)
        await self._call(
            self._client.add(
                user_id=mem0_user_id(record.workspace_id, record.actor_id),
                memory_id=str(record.id),
                content=record.content,
                metadata={
                    "kind": record.kind,
                    "subject_key": record.subject_key,
                    "revision": record.revision,
                    "workspace_id": str(record.workspace_id),
                    "actor_id": str(record.actor_id),
                },
            )
        )
        record.sync_status = MemorySyncStatus.SYNCED
        await self._session.flush()

    async def _delete(self, entry: OutboxEntry) -> None:
        record = await self._load(entry)
        await self._call(
            self._client.delete(
                user_id=mem0_user_id(record.workspace_id, record.actor_id),
                memory_id=str(record.id),
            )
        )

    async def _load(self, entry: OutboxEntry) -> MemoryRecord:
        try:
            memory_id = uuid.UUID(entry.aggregate_id)
        except ValueError as error:
            raise OutboxError(
                "MEMORY_AGGREGATE_INVALID",
                f"outbox {entry.outbox_id} aggregate {entry.aggregate_id} is not a memory id",
            ) from error
        record = await self._session.get(MemoryRecord, memory_id)
        if record is None or record.workspace_id != entry.workspace_id:
            raise OutboxError(
                "MEMORY_RECORD_MISSING",
                f"memory {memory_id} not found for outbox {entry.outbox_id}",
            )
        return record

    async def _call(self, operation: Awaitable[None]) -> None:
        """执行 Mem0 调用;任何客户端异常归一为 :class:`Mem0SyncError`。"""
        try:
            await operation
        except OutboxError:
            raise
        except Exception as error:
            raise Mem0SyncError("MEM0_UNAVAILABLE", f"mem0 call failed: {error}") from error


__all__ = ["Mem0OutboxHandler", "mem0_user_id"]
