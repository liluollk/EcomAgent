"""Mem0 Outbox Worker 与 Handler 测试(计划 04 Task 7 Step 1)。

覆盖:短租约领取与过期租约回收、失败按预算延迟重试/置 DEAD、成功后标记完成、
MEMORY_UPSERT/MEMORY_DELETE 两种事件由记忆写/删产生,以及集成失败绝不修改
Run 状态。mem0 客户端全部使用 fake,不发真实网络。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fixtures.mem0_fakes import FailingHandler, FakeMem0Client, make_entry
from sqlalchemy import select

from adapters.mem0.outbox_handler import Mem0OutboxHandler, mem0_user_id
from runtime.integrations.outbox_worker import OutboxEntry, OutboxError, OutboxWorker
from runtime.persistence.base import MemorySyncStatus, OutboxKind, OutboxStatus, RunStatus
from runtime.persistence.models.integrations import IntegrationOutbox, MemoryRecord


async def test_mem0_failure_does_not_rollback_completed_run(
    outbox_worker: OutboxWorker, completed_run, memory_upsert_outbox: IntegrationOutbox
) -> None:
    await outbox_worker.process_with_failure("MEMORY_UPSERT")
    assert await completed_run.reload_status() == RunStatus.COMPLETED


async def test_expired_outbox_lease_is_reclaimable(
    outbox_worker: OutboxWorker, expired_outbox
) -> None:
    lease = await outbox_worker.claim("worker-b")
    assert lease is not None
    assert lease.outbox_id == expired_outbox.id
    assert lease.lease_token != expired_outbox.lease_token


async def test_claim_leases_pending_outbox_with_short_lease(
    db_session, outbox_worker: OutboxWorker, memory_upsert_outbox: IntegrationOutbox
) -> None:
    lease = await outbox_worker.claim("worker-a")
    assert lease is not None
    assert lease.outbox_id == memory_upsert_outbox.id
    assert lease.lease_expires_at > datetime.now(UTC)
    await db_session.refresh(memory_upsert_outbox)
    assert memory_upsert_outbox.status == OutboxStatus.LEASED
    assert memory_upsert_outbox.lease_owner == "worker-a"
    assert memory_upsert_outbox.attempt_count == 1


async def test_claim_returns_none_when_nothing_is_due(outbox_worker: OutboxWorker) -> None:
    assert await outbox_worker.claim("worker-a") is None


async def test_successful_upsert_marks_outbox_done_and_syncs_mem0(
    db_session,
    outbox_worker: OutboxWorker,
    fake_mem0_client: FakeMem0Client,
    memory_upsert_outbox: IntegrationOutbox,
) -> None:
    lease = await outbox_worker.claim("worker-a")
    assert lease is not None
    assert await outbox_worker.process(lease) is True
    await db_session.refresh(memory_upsert_outbox)
    assert memory_upsert_outbox.status == OutboxStatus.DONE
    assert len(fake_mem0_client.added) == 1
    added = fake_mem0_client.added[0]
    assert added["memory_id"] == memory_upsert_outbox.aggregate_id
    record = await db_session.get(MemoryRecord, uuid.UUID(memory_upsert_outbox.aggregate_id))
    assert record is not None
    assert record.sync_status == MemorySyncStatus.SYNCED
    assert added["metadata"]["subject_key"] == record.subject_key
    assert added["metadata"]["revision"] == record.revision
    assert added["user_id"] == mem0_user_id(record.workspace_id, record.actor_id)


async def test_write_and_delete_enqueue_outbox_events(
    db_session, seeded_workspace, memory_service, memory_candidate
) -> None:
    memory_service.accept(memory_candidate())
    record = (
        await db_session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.workspace_id == seeded_workspace["workspace"].id
            )
        )
    ).one()
    await memory_service.delete(record.id, actor_id=record.actor_id)
    rows = list(
        (
            await db_session.scalars(
                select(IntegrationOutbox)
                .where(IntegrationOutbox.workspace_id == seeded_workspace["workspace"].id)
                .order_by(IntegrationOutbox.created_at)
            )
        ).all()
    )
    assert [row.kind for row in rows] == [
        OutboxKind.MEMORY_UPSERT,
        OutboxKind.MEMORY_DELETE,
    ]
    assert rows[1].aggregate_id == str(record.id)


async def test_successful_delete_calls_mem0_delete(
    db_session,
    outbox_worker: OutboxWorker,
    fake_mem0_client: FakeMem0Client,
    memory_service,
    memory_candidate,
) -> None:
    memory_service.accept(memory_candidate())
    record = (
        await db_session.scalars(select(MemoryRecord).limit(1))
    ).one()
    await memory_service.delete(record.id, actor_id=record.actor_id)
    delete_row = (
        await db_session.scalars(
            select(IntegrationOutbox).where(
                IntegrationOutbox.kind == OutboxKind.MEMORY_DELETE
            )
        )
    ).one()
    lease = await outbox_worker.claim("worker-a")
    assert lease is not None
    # claim 按 available_at FIFO:先处理 MEMORY_UPSERT,再领取 MEMORY_DELETE。
    assert await outbox_worker.process(lease) is True
    delete_lease = await outbox_worker.claim("worker-a")
    assert delete_lease is not None
    assert delete_lease.outbox_id == delete_row.id
    assert await outbox_worker.process(delete_lease) is True
    await db_session.refresh(delete_row)
    assert delete_row.status == OutboxStatus.DONE
    assert fake_mem0_client.deleted == [
        {"user_id": mem0_user_id(record.workspace_id, record.actor_id), "memory_id": str(record.id)}
    ]


async def test_failed_handler_requeues_with_backoff(
    db_session, memory_upsert_outbox: IntegrationOutbox
) -> None:
    worker = OutboxWorker(
        db_session, handler=FailingHandler(), retry_delay=timedelta(minutes=1)
    )
    lease = await worker.claim("worker-a")
    assert lease is not None
    assert await worker.process(lease) is False
    await db_session.refresh(memory_upsert_outbox)
    assert memory_upsert_outbox.status == OutboxStatus.PENDING
    assert memory_upsert_outbox.attempt_count == 1
    assert memory_upsert_outbox.available_at > datetime.now(UTC)
    assert memory_upsert_outbox.last_error_code == "HANDLER_FAILURE"
    assert memory_upsert_outbox.lease_token is None


async def test_attempt_budget_exhaustion_marks_outbox_dead(
    db_session, memory_upsert_outbox: IntegrationOutbox
) -> None:
    worker = OutboxWorker(db_session, handler=FailingHandler(), max_attempts=1)
    lease = await worker.claim("worker-a")
    assert lease is not None
    assert await worker.process(lease) is False
    await db_session.refresh(memory_upsert_outbox)
    assert memory_upsert_outbox.status == OutboxStatus.DEAD
    assert memory_upsert_outbox.last_error_code == "HANDLER_FAILURE"
    assert memory_upsert_outbox.lease_token is None


async def test_handler_normalizes_mem0_failure(
    db_session, memory_upsert_outbox: IntegrationOutbox
) -> None:
    handler = Mem0OutboxHandler(db_session, FakeMem0Client(fail_on="add"))
    with pytest.raises(OutboxError):
        await handler.handle(make_entry(memory_upsert_outbox))


async def test_handler_upserts_payload_with_local_metadata(
    db_session, fake_mem0_client: FakeMem0Client, memory_upsert_outbox: IntegrationOutbox
) -> None:
    handler = Mem0OutboxHandler(db_session, fake_mem0_client)
    await handler.handle(make_entry(memory_upsert_outbox))
    assert len(fake_mem0_client.added) == 1


async def test_handler_ignores_unknown_kinds(
    fake_mem0_client: FakeMem0Client,
) -> None:
    handler = Mem0OutboxHandler(None, fake_mem0_client)  # type: ignore[arg-type]
    entry = OutboxEntry(
        outbox_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        kind=OutboxKind.TRACE_EXPORT,
        aggregate_type="other",
        aggregate_id=str(uuid.uuid4()),
        attempt_count=0,
    )
    with pytest.raises(OutboxError):
        await handler.handle(entry)
