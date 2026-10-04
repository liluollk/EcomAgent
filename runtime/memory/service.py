"""MemoryService 应用服务(计划 04 Task 7 Step 3;架构文档 §7)。

权威语义:
- Runtime 只保存已确认的偏好/反馈/决策/经验;执行关键事实(权限、资源
  范围、价格规则、凭据)与 Secret/凭据形态的内容绝不入库;
- 纠正创建新 Revision 并替代旧记录(旧记录置 SUPERSEDED);
- 删除写 Tombstone(deleted_at + DELETED),不做物理删除;
- 写入/纠正/删除在同一事务内产生 MEMORY_UPSERT/MEMORY_DELETE Outbox 行;
- Mem0 检索结果是带来源的不可信数据,必须按本地 ID、Revision、生命周期与
  有效期过滤后才可注入 Context;不做模型自动冲突裁决;
- 服务只 flush 不 commit,事务边界归调用方;全部读写按 workspace/actor 隔离。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.memory.lifecycle import (
    INITIAL_REVISION,
    MemoryLifecycleError,
    MemoryLifecycleStatus,
    assert_transition,
    next_revision,
)
from runtime.persistence.base import MemorySyncStatus, OutboxKind
from runtime.persistence.models.integrations import IntegrationOutbox, MemoryRecord


ACCEPTED_MEMORY_KINDS: Final[frozenset[str]] = frozenset(
    {"preference", "feedback", "decision", "experience"}
)
"""Runtime 允许入库的已确认记忆类型(架构文档 §7)。"""

EXECUTION_CRITICAL_MEMORY_KINDS: Final[frozenset[str]] = frozenset(
    {"permission", "resource_scope", "price_rule", "credential"}
)
"""执行关键事实:禁止作为长期记忆入库(计划 04 Task 7)。"""

_SECRET_CONTENT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
"""保守的 Secret 形态探测器:命中的内容无论 kind 一律拒绝入库。"""


class MemoryServiceError(Exception):
    """Memory 领域错误基类;携带稳定状态码与错误码供 API 翻译。"""

    status_code: int = 400
    code: str = "MEMORY_ERROR"


class MemoryAccessDenied(MemoryServiceError):
    """当前 actor 不是记忆的属主(403 PERMISSION_DENIED)。"""

    status_code = 403
    code = "PERMISSION_DENIED"

    def __init__(self, message: str = "无权访问他人的记忆") -> None:
        super().__init__(message)


class MemoryNotFoundError(MemoryServiceError):
    """记忆在当前 workspace 内不存在或不属于当前 actor 视野外(404)。"""

    status_code = 404
    code = "NOT_FOUND"

    def __init__(self, memory_id: uuid.UUID) -> None:
        super().__init__(f"memory {memory_id} not found")


class MemoryContentRejected(MemoryServiceError):
    """纠正内容包含 Secret/凭据形态数据,拒绝写入(422)。"""

    status_code = 422
    code = "VALIDATION_ERROR"


@dataclass(frozen=True)
class MemoryCandidate:
    """待入库的已确认记忆候选。"""

    kind: str
    content: str
    subject_key: str
    actor_id: uuid.UUID
    source_type: str = "CONFIRMED"
    source_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class MemoryHit:
    """Mem0 检索结果归一视图;``memory_id`` 是本地权威记录 ID。"""

    memory_id: str
    revision: int | None = None
    content: str | None = None
    score: float | None = None


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _looks_like_secret(content: str) -> bool:
    return any(pattern.search(content) for pattern in _SECRET_CONTENT_PATTERNS)


class MemoryService:
    """记忆权威记录应用服务;在调用方事务内执行,只 flush 不 commit。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # 入库策略
    # ------------------------------------------------------------------

    def accept(self, candidate: MemoryCandidate) -> bool:
        """按策略入库已确认记忆;执行关键事实/Secret 形态返回 False。

        成功时写入 ACTIVE 的权威记录并在同一事务内产生 MEMORY_UPSERT
        Outbox 行;只 add 不 flush,由调用方事务统一次提交。
        """
        if candidate.kind in EXECUTION_CRITICAL_MEMORY_KINDS:
            return False
        if candidate.kind not in ACCEPTED_MEMORY_KINDS:
            return False
        if _looks_like_secret(candidate.content):
            return False
        record = MemoryRecord(
            id=uuid.uuid4(),
            workspace_id=self._workspace_id,
            actor_id=candidate.actor_id,
            kind=candidate.kind,
            subject_key=candidate.subject_key,
            content=candidate.content,
            content_hash=_content_hash(candidate.content),
            source_type=candidate.source_type,
            source_id=candidate.source_id,
            revision=INITIAL_REVISION,
            lifecycle_status=MemoryLifecycleStatus.ACTIVE,
            sync_status=MemorySyncStatus.PENDING,
            expires_at=candidate.expires_at,
        )
        self._session.add_all([record, self._upsert_outbox(record)])
        return True

    # ------------------------------------------------------------------
    # 纠正与 Tombstone
    # ------------------------------------------------------------------

    async def correct(self, memory_id: uuid.UUID, *, content: str) -> MemoryRecord:
        """纠正记忆:创建新 Revision 替代旧记录;旧记录置 SUPERSEDED。"""
        if _looks_like_secret(content):
            raise MemoryContentRejected("纠正内容包含疑似凭据,拒绝写入")
        actor_id = self._require_actor()
        record = await self._get_visible(memory_id, actor_id)
        if record is None:
            raise MemoryNotFoundError(memory_id)
        assert_transition(record.lifecycle_status, MemoryLifecycleStatus.SUPERSEDED)
        corrected = MemoryRecord(
            id=uuid.uuid4(),
            workspace_id=self._workspace_id,
            actor_id=record.actor_id,
            kind=record.kind,
            subject_key=record.subject_key,
            content=content,
            content_hash=_content_hash(content),
            source_type=record.source_type,
            source_id=record.source_id,
            source_session_id=record.source_session_id,
            revision=next_revision(record.revision),
            supersedes_id=record.id,
            lifecycle_status=MemoryLifecycleStatus.ACTIVE,
            sync_status=MemorySyncStatus.PENDING,
        )
        record.lifecycle_status = MemoryLifecycleStatus.SUPERSEDED
        self._session.add_all([corrected, self._upsert_outbox(corrected)])
        await self._session.flush()
        return corrected

    async def delete(
        self, memory_id: uuid.UUID, *, actor_id: uuid.UUID | None = None
    ) -> MemoryRecord:
        """删除记忆:写 Tombstone(deleted_at + DELETED)并产生 MEMORY_DELETE。"""
        effective_actor = actor_id if actor_id is not None else self._require_actor()
        record = await self._get_visible(memory_id, effective_actor)
        if record is None:
            raise MemoryNotFoundError(memory_id)
        assert_transition(record.lifecycle_status, MemoryLifecycleStatus.DELETED)
        record.lifecycle_status = MemoryLifecycleStatus.DELETED
        record.deleted_at = self._now()
        record.deleted_by_actor_id = effective_actor
        self._session.add(self._delete_outbox(record))
        await self._session.flush()
        return record

    # ------------------------------------------------------------------
    # 检索过滤与到期维护
    # ------------------------------------------------------------------

    async def filter_retrieval(self, hits: Sequence[MemoryHit]) -> list[MemoryHit]:
        """按本地 ID、Revision、生命周期与有效期过滤 Mem0 检索结果。

        只保留:本地存在、属于当前 workspace 与 actor、生命周期为 ACTIVE、
        未到期、且 Revision 与本地权威记录一致的命中。其余一律丢弃。
        """
        actor_id = self._require_actor()
        now = self._now()
        kept: list[MemoryHit] = []
        for hit in hits:
            try:
                memory_id = uuid.UUID(hit.memory_id)
            except ValueError:
                continue
            record = await self._session.get(MemoryRecord, memory_id)
            if record is None or record.workspace_id != self._workspace_id:
                continue
            if record.actor_id != actor_id:
                continue
            if record.lifecycle_status is not MemoryLifecycleStatus.ACTIVE:
                continue
            if record.expires_at is not None and record.expires_at <= now:
                continue
            if hit.revision is not None and hit.revision != record.revision:
                continue
            kept.append(hit)
        return kept

    async def expire_due(self, *, now: datetime | None = None) -> int:
        """把已到期的 ACTIVE 记录置为 EXPIRED;返回受影响行数。

        由 Worker 维护任务调用;``now`` 供测试注入。
        """
        cutoff = now if now is not None else self._now()
        stmt = (
            sa.update(MemoryRecord)
            .where(
                MemoryRecord.workspace_id == self._workspace_id,
                MemoryRecord.lifecycle_status == MemoryLifecycleStatus.ACTIVE,
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at <= cutoff,
            )
            .values(lifecycle_status=MemoryLifecycleStatus.EXPIRED)
            .execution_options(synchronize_session=False)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return int(result.rowcount or 0)

    async def list_own(self, *, limit: int = 100) -> list[MemoryRecord]:
        """当前 actor 在本 workspace 内的全部记忆(含生命周期状态)。"""
        self._require_actor()
        stmt = (
            select(MemoryRecord)
            .where(
                MemoryRecord.workspace_id == self._workspace_id,
                MemoryRecord.actor_id == self._actor_id,
            )
            .order_by(MemoryRecord.created_at.desc(), MemoryRecord.id)
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _require_actor(self) -> uuid.UUID:
        if self._actor_id is None:
            raise MemoryAccessDenied("memory service requires a bound actor")
        return self._actor_id

    async def _get_visible(
        self, memory_id: uuid.UUID, actor_id: uuid.UUID
    ) -> MemoryRecord | None:
        """读取 workspace 内的记忆;存在但属于他人时抛 MemoryAccessDenied。"""
        record = await self._session.get(MemoryRecord, memory_id)
        if record is None or record.workspace_id != self._workspace_id:
            return None
        if record.actor_id != actor_id:
            raise MemoryAccessDenied()
        return record

    def _upsert_outbox(self, record: MemoryRecord) -> IntegrationOutbox:
        return IntegrationOutbox(
            workspace_id=self._workspace_id,
            kind=OutboxKind.MEMORY_UPSERT,
            aggregate_type="memory_record",
            aggregate_id=str(record.id),
            payload_ref=f"memory_records/{record.id}",
        )

    def _delete_outbox(self, record: MemoryRecord) -> IntegrationOutbox:
        return IntegrationOutbox(
            workspace_id=self._workspace_id,
            kind=OutboxKind.MEMORY_DELETE,
            aggregate_type="memory_record",
            aggregate_id=str(record.id),
            payload_ref=f"memory_records/{record.id}",
        )


__all__ = [
    "ACCEPTED_MEMORY_KINDS",
    "EXECUTION_CRITICAL_MEMORY_KINDS",
    "MemoryAccessDenied",
    "MemoryCandidate",
    "MemoryContentRejected",
    "MemoryHit",
    "MemoryLifecycleError",
    "MemoryNotFoundError",
    "MemoryService",
    "MemoryServiceError",
]
