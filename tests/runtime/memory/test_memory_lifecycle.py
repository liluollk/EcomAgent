"""记忆生命周期测试(计划 04 Task 7 Step 1)。

覆盖:纠正替代(新 Revision + SUPERSEDED)、Tombstone 过滤、跨 actor 删除拒绝、
到期记录 expire_due() 置 EXPIRED,以及检索结果按本地 ID/Revision/生命周期/
有效期的过滤。
"""

import uuid
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from runtime.memory.service import MemoryAccessDenied, MemoryNotFoundError, MemoryService
from runtime.persistence.models.identity import User
from runtime.persistence.models.integrations import MemoryRecord


async def test_correction_supersedes_previous_memory(
    memory_service, active_memory
) -> None:
    corrected = await memory_service.correct(
        active_memory.id, content="prefer cautious changes"
    )
    assert corrected.supersedes_id == active_memory.id
    assert (await active_memory.reload()).lifecycle_status == "SUPERSEDED"


async def test_correction_increments_revision_and_keeps_subject(
    memory_service, active_memory
) -> None:
    previous_revision = active_memory.record.revision
    subject_key = active_memory.record.subject_key
    corrected = await memory_service.correct(
        active_memory.id, content="prefer cautious changes"
    )
    assert corrected.revision == previous_revision + 1
    assert corrected.subject_key == subject_key
    assert corrected.content == "prefer cautious changes"


async def test_correcting_missing_memory_is_not_found(memory_service) -> None:
    with pytest.raises(MemoryNotFoundError):
        await memory_service.correct(uuid.uuid4(), content="prefer cautious changes")


async def test_correcting_superseded_memory_is_rejected(
    memory_service, active_memory
) -> None:
    await memory_service.correct(active_memory.id, content="prefer cautious changes")
    with pytest.raises(Exception, match="lifecycle"):
        await memory_service.correct(active_memory.id, content="another correction")


async def test_deleted_memory_is_filtered_even_if_mem0_returns_it(
    memory_service, active_memory, mem0_hit
) -> None:
    await memory_service.delete(active_memory.id, actor_id=active_memory.actor_id)
    recalled = await memory_service.filter_retrieval([mem0_hit(active_memory.id)])
    assert recalled == []


async def test_actor_cannot_delete_another_users_memory(
    memory_service, active_memory
) -> None:
    with pytest.raises(MemoryAccessDenied):
        await memory_service.delete(active_memory.id, actor_id=uuid4())


async def test_delete_writes_tombstone(memory_service, active_memory) -> None:
    deleted = await memory_service.delete(
        active_memory.id, actor_id=active_memory.actor_id
    )
    assert deleted.lifecycle_status == "DELETED"
    assert deleted.deleted_at is not None
    assert deleted.deleted_by_actor_id == active_memory.actor_id


async def test_deleting_missing_memory_is_not_found(memory_service, seeded_workspace) -> None:
    with pytest.raises(MemoryNotFoundError):
        await memory_service.delete(uuid.uuid4(), actor_id=seeded_workspace.user.id)


async def test_deleting_deleted_memory_is_rejected(memory_service, active_memory) -> None:
    await memory_service.delete(active_memory.id, actor_id=active_memory.actor_id)
    with pytest.raises(Exception, match="lifecycle"):
        await memory_service.delete(active_memory.id, actor_id=active_memory.actor_id)


async def test_expire_due_marks_due_records_expired(
    memory_service,
    db_session,
    seeded_workspace,
    memory_candidate,
) -> None:
    memory_service.accept(
        memory_candidate(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    expired_count = await memory_service.expire_due()
    assert expired_count == 1
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace.workspace.id
            )
        )
    ).one()
    assert record.lifecycle_status == "EXPIRED"


async def test_expire_due_keeps_future_records(memory_service, memory_candidate) -> None:
    memory_service.accept(
        memory_candidate(expires_at=datetime.now(UTC) + timedelta(hours=1))
    )
    assert await memory_service.expire_due() == 0


async def test_expired_memory_is_filtered_from_retrieval(
    memory_service, db_session, seeded_workspace, memory_candidate, mem0_hit
) -> None:
    memory_service.accept(
        memory_candidate(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace.workspace.id
            )
        )
    ).one()
    await memory_service.expire_due()
    assert await memory_service.filter_retrieval([mem0_hit(record.id)]) == []


async def test_superseded_memory_is_filtered_from_retrieval(
    memory_service, active_memory, mem0_hit
) -> None:
    await memory_service.correct(active_memory.id, content="prefer cautious changes")
    stale_hit = mem0_hit(active_memory.id, revision=1)
    assert await memory_service.filter_retrieval([stale_hit]) == []


async def test_stale_revision_hits_are_filtered(
    memory_service, active_memory, mem0_hit
) -> None:
    corrected = await memory_service.correct(
        active_memory.id, content="prefer cautious changes"
    )
    recalled = await memory_service.filter_retrieval(
        [
            mem0_hit(active_memory.id, revision=1),
            mem0_hit(corrected.id, revision=corrected.revision),
        ]
    )
    assert recalled == [mem0_hit(corrected.id, revision=corrected.revision)]


async def test_unknown_memory_hit_is_filtered(memory_service, mem0_hit) -> None:
    assert await memory_service.filter_retrieval([mem0_hit(uuid.uuid4())]) == []


async def test_retrieval_filters_other_actors_memories(
    db_session, seeded_workspace, memory_candidate, mem0_hit
) -> None:
    other = User(
        id=uuid.uuid4(),
        login=f"other-{uuid.uuid4().hex[:12]}",
        password_hash="test-hash",
    )
    db_session.add(other)
    await db_session.flush()
    other_service = MemoryService(
        db_session, workspace_id=seeded_workspace.workspace.id, actor_id=other.id
    )
    other_service.accept(memory_candidate(actor_id=other.id))
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace.workspace.id,
                MemoryRecord.actor_id == other.id,
            )
        )
    ).one()
    recalled = await other_service.filter_retrieval([mem0_hit(record.id)])
    assert recalled == [mem0_hit(record.id)]
    owner_service = MemoryService(
        db_session,
        workspace_id=seeded_workspace.workspace.id,
        actor_id=seeded_workspace.user.id,
    )
    assert await owner_service.filter_retrieval([mem0_hit(record.id)]) == []
