"""Mem0 客户端 Protocol 与 OSS 实现(计划 04 Task 7 Step 3)。

Runtime 与 Handler 只依赖 :class:`Mem0Client` Protocol,不依赖 mem0ai 的
具体类型;测试注入 ``tests/fixtures/mem0_fakes.py`` 的内存 fake,不发真实
网络。外部索引(Mem0)不是权威源,检索结果必须经
``MemoryService.filter_retrieval`` 按本地 ID/Revision/生命周期/有效期过滤
后才可注入 Context。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from runtime.integrations.outbox_worker import OutboxError
from runtime.memory.service import MemoryHit


@runtime_checkable
class Mem0Client(Protocol):
    """Mem0 外部记忆索引的最小能力面:add / search / delete。"""

    async def add(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """按 user_id 命名空间写入/更新一条外部记忆,携带本地元数据。"""
        ...

    async def search(
        self, *, user_id: str, query: str, limit: int = 5
    ) -> list[MemoryHit]:
        """按 user_id 检索相关记忆;结果必须再经本地过滤。"""
        ...

    async def delete(self, *, user_id: str, memory_id: str) -> None:
        """按本地记忆 ID 删除外部记忆(Tombstone 同步)。"""
        ...


class Mem0SyncError(OutboxError):
    """Mem0 调用失败的归一错误;由 OutboxWorker 统一按预算重试。"""


def _to_hit(item: Mapping[str, Any]) -> MemoryHit:
    """把 mem0 检索结果归一为 :class:`MemoryHit`。

    外部条目通过 metadata 中的 ``local_memory_id`` / ``revision`` 关联本地
    权威记录;缺失时 revision 视为未知,由本地过滤兜底。
    """
    metadata: Mapping[str, Any] = item.get("metadata") or {}
    local_id = metadata.get("local_memory_id") or item.get("id") or ""
    revision = metadata.get("revision")
    score = item.get("score")
    return MemoryHit(
        memory_id=str(local_id),
        revision=int(revision) if revision is not None else None,
        content=item.get("memory") if isinstance(item.get("memory"), str) else None,
        score=float(score) if score is not None else None,
    )


class Mem0OssClient:
    """mem0ai OSS ``Memory`` 的薄封装;阻塞 SDK 调用经 ``to_thread`` 执行。

    构造参数为 mem0 配置(provider/vector store/embedder);缺省使用本地
    默认配置。客户端把任何 SDK 异常归一为 :class:`Mem0SyncError`。
    """

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._config: dict[str, Any] = dict(config or {})
        self._memory: Any | None = None

    def _ensure_memory(self) -> Any:
        if self._memory is None:
            from mem0 import Memory  # type: ignore[import-untyped]

            self._memory = (
                Memory.from_config(self._config) if self._config else Memory()
            )
        return self._memory

    async def add(
        self,
        *,
        user_id: str,
        memory_id: str,
        content: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        memory = self._ensure_memory()
        payload = [{"role": "user", "content": content}]
        merged = {"local_memory_id": memory_id, **dict(metadata or {})}
        try:
            await asyncio.to_thread(memory.add, payload, user_id=user_id, metadata=merged)
        except OutboxError:
            raise
        except Exception as error:  # pragma: no cover - 真实 SDK 路径,测试用 fake
            raise Mem0SyncError("MEM0_UNAVAILABLE", f"mem0 add failed: {error}") from error

    async def search(
        self, *, user_id: str, query: str, limit: int = 5
    ) -> list[MemoryHit]:
        memory = self._ensure_memory()
        try:
            raw = await asyncio.to_thread(memory.search, query, user_id=user_id, limit=limit)
        except OutboxError:
            raise
        except Exception as error:  # pragma: no cover - 真实 SDK 路径,测试用 fake
            raise Mem0SyncError("MEM0_UNAVAILABLE", f"mem0 search failed: {error}") from error
        results: list[Mapping[str, Any]] = list((raw or {}).get("results") or [])
        return [_to_hit(item) for item in results]

    async def delete(self, *, user_id: str, memory_id: str) -> None:
        memory = self._ensure_memory()
        try:
            await asyncio.to_thread(memory.delete, memory_id)
        except OutboxError:
            raise
        except Exception as error:  # pragma: no cover - 真实 SDK 路径,测试用 fake
            raise Mem0SyncError("MEM0_UNAVAILABLE", f"mem0 delete failed: {error}") from error


__all__ = ["Mem0Client", "Mem0OssClient", "Mem0SyncError"]
