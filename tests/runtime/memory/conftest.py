"""Memory 策略与生命周期测试共享夹具(计划 04 Task 7)。

提供:
- ``memory_service``:绑定种子 workspace 与当前 actor 的 MemoryService;
- ``memory_candidate``:构造合法 MemoryCandidate 的工厂;
- ``active_memory``:经 accept 落库的活动记忆(计划原文测试使用 reload);
- ``mem0_hit``:构造 Mem0 检索结果命中的工厂。
"""

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.memory.service import MemoryCandidate, MemoryHit, MemoryService
from runtime.persistence.models.integrations import MemoryRecord


@dataclass
class MemoryRef:
    """计划原文测试使用的记忆引用:暴露 id/actor_id 并支持 reload。"""

    record: MemoryRecord
    session: AsyncSession

    @property
    def id(self) -> uuid.UUID:
        return self.record.id

    @property
    def actor_id(self) -> uuid.UUID:
        return self.record.actor_id

    async def reload(self) -> MemoryRecord:
        await self.session.refresh(self.record)
        return self.record


@pytest.fixture()
def memory_service(db_session: AsyncSession, seeded_workspace: Any) -> MemoryService:
    return MemoryService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )


@pytest.fixture()
def memory_candidate(seeded_workspace: Any) -> Any:
    actor_id = seeded_workspace.user.id

    def _candidate(**overrides: Any) -> MemoryCandidate:
        values: dict[str, Any] = {
            "kind": "preference",
            "content": "prefer short replies",
            "subject_key": f"user-{uuid.uuid4()}",
            "actor_id": actor_id,
        }
        values.update(overrides)
        return MemoryCandidate(**values)

    return _candidate


@pytest.fixture()
async def active_memory(
    db_session: AsyncSession,
    memory_service: MemoryService,
    seeded_workspace: Any,
    memory_candidate: Any,
) -> MemoryRef:
    """一条经 accept 落库的活动记忆(revision 1、ACTIVE)。"""
    assert memory_service.accept(memory_candidate()) is True
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace.workspace.id
            )
        )
    ).one()
    return MemoryRef(record=record, session=db_session)


@pytest.fixture()
def mem0_hit() -> Any:
    def _hit(
        memory_id: str | uuid.UUID,
        *,
        revision: int = 1,
        content: str = "prefer short replies",
    ) -> MemoryHit:
        return MemoryHit(
            memory_id=str(memory_id), revision=revision, content=content, score=0.9
        )

    return _hit
