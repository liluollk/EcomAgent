"""Mem0 Outbox 测试替身(计划 04 Task 7)。

全部为内存实现:测试不发真实网络。``FakeMem0Client`` 记录调用并可注入失败,
``FailingHandler`` 始终抛出 :class:`OutboxError` 以驱动重试/DEAD 路径。
"""

from typing import Any

from runtime.integrations.outbox_worker import OutboxEntry, OutboxError
from runtime.persistence.models.integrations import IntegrationOutbox


class FakeMem0Client:
    """内存版 Mem0Client:记录调用并可注入失败。"""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.added: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.searched: list[dict[str, Any]] = []
        self._fail_on = fail_on

    async def add(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._fail_on == "add":
            raise OutboxError("MEM0_UNAVAILABLE", "injected add failure")
        self.added.append(
            {
                "user_id": user_id,
                "memory_id": memory_id,
                "content": content,
                "metadata": dict(metadata or {}),
            }
        )

    async def search(
        self, *, user_id: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        if self._fail_on == "search":
            raise OutboxError("MEM0_UNAVAILABLE", "injected search failure")
        self.searched.append({"user_id": user_id, "query": query, "limit": limit})
        return []

    async def delete(self, *, user_id: str, memory_id: str) -> None:
        if self._fail_on == "delete":
            raise OutboxError("MEM0_UNAVAILABLE", "injected delete failure")
        self.deleted.append({"user_id": user_id, "memory_id": memory_id})


class FailingHandler:
    """始终失败的 OutboxHandler;错误码固定为 HANDLER_FAILURE。"""

    async def handle(self, entry: OutboxEntry) -> None:
        raise OutboxError("HANDLER_FAILURE", "injected handler failure")


def make_entry(row: IntegrationOutbox) -> OutboxEntry:
    """把 Outbox 行包装成 handler 入参。"""
    return OutboxEntry(
        outbox_id=row.id,
        workspace_id=row.workspace_id,
        kind=row.kind,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        attempt_count=row.attempt_count,
    )
