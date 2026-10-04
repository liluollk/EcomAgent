"""Langfuse Trace Outbox 测试(计划 04 Task 8 Step 1)。

覆盖计划原文两测(payload 脱敏、Langfuse 失败不改 Run),并补充:
- Run/Tool/Request 事件 → trace/span 关联映射(run_id 为 trace 根);
- TraceRedactor 对嵌套 payload、已知敏感键与密钥形态值的字段级脱敏;
- enqueue_trace_export + OutboxWorker 全链路:成功 DONE、失败重试/DEAD;
- Langfuse 失败归一为可重试/不可重试,绝不读取或修改 Run 状态。

Langfuse 客户端全部使用内存 fake,不发真实网络。
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from adapters.langfuse.client import LangfuseInvalidTraceError, LangfuseUnavailableError
from adapters.langfuse.exporter import TraceEventSnapshot
from adapters.langfuse.outbox_handler import LangfuseOutboxHandler
from runtime.integrations.outbox_worker import OutboxEntry, OutboxError, OutboxWorker
from runtime.observability.redaction import TraceRedactor
from runtime.observability.trace_outbox import enqueue_trace_export
from runtime.persistence.base import OutboxKind, OutboxStatus, RunStatus
from runtime.persistence.models.integrations import IntegrationOutbox


SECRET_IN_DB = "sk-testsecret12345"


def _make_entry(
    *, workspace_id: uuid.UUID, run_id: uuid.UUID, kind: OutboxKind = OutboxKind.TRACE_EXPORT
) -> OutboxEntry:
    return OutboxEntry(
        outbox_id=uuid.uuid4(),
        workspace_id=workspace_id,
        kind=kind,
        aggregate_type="run",
        aggregate_id=str(run_id),
        attempt_count=0,
    )


async def _trace_outbox_row(db_session, run_id: uuid.UUID) -> IntegrationOutbox:
    row = (
        await db_session.scalars(
            select(IntegrationOutbox).where(
                IntegrationOutbox.kind == OutboxKind.TRACE_EXPORT,
                IntegrationOutbox.aggregate_id == str(run_id),
            )
        )
    ).one()
    await db_session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# 计划原文测试
# ---------------------------------------------------------------------------


async def test_trace_payload_is_redacted(trace_exporter, event_with_secret) -> None:
    payload = trace_exporter.build_payload(event_with_secret("sk-sensitive"))
    assert "sk-sensitive" not in json.dumps(payload)
    assert payload["attributes"]["run_id"] == "run-1"


async def test_langfuse_failure_never_changes_run(trace_outbox, completed_run) -> None:
    await trace_outbox.process_with_failure(completed_run.id)
    assert await completed_run.reload_status() == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Trace 结构映射:run_id 为 trace 根,tool/request 事件为根下 span
# ---------------------------------------------------------------------------


async def test_run_events_map_to_trace_root(trace_exporter) -> None:
    payload = trace_exporter.build_payload(
        TraceEventSnapshot(
            event_type="RUN_STARTED",
            run_id="run-1",
            session_id="sess-1",
            sequence_no=1,
            payload={"trigger": "manual"},
            attempt_id="attempt-1",
        )
    )
    assert payload["type"] == "trace-create"
    assert payload["id"] == "run-1"
    assert payload["traceId"] == "run-1"
    assert payload["attributes"]["run_id"] == "run-1"
    assert payload["attributes"]["session_id"] == "sess-1"
    assert payload["attributes"]["attempt_id"] == "attempt-1"
    assert payload["body"]["name"] == "RUN_STARTED"
    assert payload["body"]["input"] == {"trigger": "manual"}


async def test_tool_and_request_events_map_to_run_spans(trace_exporter) -> None:
    tool_span = trace_exporter.build_payload(
        TraceEventSnapshot(
            event_type="TOOL_RESULT_RECORDED",
            run_id="run-1",
            session_id="sess-1",
            sequence_no=2,
            payload={"tool": "order.search"},
            tool_call_id="tool-9",
        )
    )
    request_span = trace_exporter.build_payload(
        TraceEventSnapshot(
            event_type="REQUEST_RESOLVED",
            run_id="run-1",
            session_id="sess-1",
            sequence_no=3,
            payload={"request": "confirm"},
        )
    )
    for span, observation_id in ((tool_span, "run-1:2"), (request_span, "run-1:3")):
        assert span["type"] == "span-create"
        assert span["traceId"] == "run-1"
        assert span["id"] == observation_id
        assert span["attributes"]["run_id"] == "run-1"
        assert span["body"]["parentObservationId"] == "run-1"
    assert tool_span["attributes"]["tool_call_id"] == "tool-9"


# ---------------------------------------------------------------------------
# 脱敏规则:敏感键名 + 密钥形态值,嵌套覆盖,不改输入
# ---------------------------------------------------------------------------


def test_trace_redactor_scrubs_nested_payload_and_sensitive_keys() -> None:
    redactor = TraceRedactor()
    original = {
        "messages": [{"role": "user", "content": "call with sk-abcdef123456 now"}],
        "api_key": "plain-secret-value",
        "nested": {"client_secret": "s3cr3t-value", "run": "run-1"},
        "count": 2,
    }
    snapshot = json.dumps(original)
    redacted = redactor.redact(original)
    dumped = json.dumps(redacted)
    assert "sk-abcdef123456" not in dumped
    assert "plain-secret-value" not in dumped
    assert "s3cr3t-value" not in dumped
    assert redacted["messages"][0]["role"] == "user"
    assert redacted["nested"]["run"] == "run-1"
    assert redacted["count"] == 2
    assert json.dumps(original) == snapshot  # 纯函数:不修改输入


# ---------------------------------------------------------------------------
# Outbox 挂接与全链路(enqueue + OutboxWorker 驱动)
# ---------------------------------------------------------------------------


async def test_enqueue_creates_pending_trace_export_row(db_session, completed_run) -> None:
    row = await enqueue_trace_export(
        db_session, workspace_id=completed_run.run.workspace_id, run_id=completed_run.id
    )
    await db_session.refresh(row)
    assert row.kind == OutboxKind.TRACE_EXPORT
    assert row.status == OutboxStatus.PENDING
    assert row.aggregate_type == "run"
    assert row.aggregate_id == str(completed_run.id)
    assert row.payload_ref == f"run:{completed_run.id}"


async def test_successful_export_marks_outbox_done_and_sends_redacted_batch(
    db_session, outbox_worker, fake_langfuse_client, run_with_events
) -> None:
    await enqueue_trace_export(
        db_session, workspace_id=run_with_events.run.workspace_id, run_id=run_with_events.id
    )
    lease = await outbox_worker.claim("worker-a")
    assert lease is not None
    assert await outbox_worker.process(lease) is True
    row = await _trace_outbox_row(db_session, run_with_events.id)
    assert row.status == OutboxStatus.DONE
    assert len(fake_langfuse_client.batches) == 1
    batch = fake_langfuse_client.batches[0]
    assert [item["type"] for item in batch] == ["trace-create", "span-create"]
    assert batch[0]["id"] == str(run_with_events.id)
    assert batch[1]["traceId"] == str(run_with_events.id)
    assert batch[1]["attributes"]["run_id"] == str(run_with_events.id)
    assert SECRET_IN_DB not in json.dumps(batch)
    assert await run_with_events.reload_status() == RunStatus.COMPLETED


async def test_langfuse_unavailable_requeues_outbox_without_changing_run(
    db_session, failing_langfuse_client, run_with_events
) -> None:
    await enqueue_trace_export(
        db_session, workspace_id=run_with_events.run.workspace_id, run_id=run_with_events.id
    )
    worker = OutboxWorker(
        db_session, handler=LangfuseOutboxHandler(db_session, failing_langfuse_client)
    )
    lease = await worker.claim("worker-a")
    assert lease is not None
    assert await worker.process(lease) is False
    row = await _trace_outbox_row(db_session, run_with_events.id)
    assert row.status == OutboxStatus.PENDING
    assert row.attempt_count == 1
    assert row.last_error_code == "LANGFUSE_UNAVAILABLE"
    assert row.lease_token is None
    assert row.available_at > datetime.now(UTC)
    assert await run_with_events.reload_status() == RunStatus.COMPLETED


async def test_attempt_budget_exhaustion_marks_trace_outbox_dead(
    db_session, failing_langfuse_client, run_with_events
) -> None:
    await enqueue_trace_export(
        db_session, workspace_id=run_with_events.run.workspace_id, run_id=run_with_events.id
    )
    worker = OutboxWorker(
        db_session,
        handler=LangfuseOutboxHandler(db_session, failing_langfuse_client),
        max_attempts=1,
    )
    lease = await worker.claim("worker-a")
    assert lease is not None
    assert await worker.process(lease) is False
    row = await _trace_outbox_row(db_session, run_with_events.id)
    assert row.status == OutboxStatus.DEAD
    assert row.last_error_code == "LANGFUSE_UNAVAILABLE"
    assert await run_with_events.reload_status() == RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Handler 失败归一:可重试 / 不可重试 / 不支持的 kind
# ---------------------------------------------------------------------------


async def test_handler_normalizes_client_failure_as_retryable(
    db_session, failing_langfuse_client, run_with_events
) -> None:
    handler = LangfuseOutboxHandler(db_session, failing_langfuse_client)
    with pytest.raises(LangfuseUnavailableError) as excinfo:
        await handler.handle(
            _make_entry(workspace_id=run_with_events.run.workspace_id, run_id=run_with_events.id)
        )
    assert excinfo.value.code == "LANGFUSE_UNAVAILABLE"
    assert excinfo.value.retryable is True


async def test_missing_run_events_is_permanent_error(db_session, fake_langfuse_client, completed_run) -> None:
    handler = LangfuseOutboxHandler(db_session, fake_langfuse_client)
    with pytest.raises(LangfuseInvalidTraceError) as excinfo:
        await handler.handle(
            _make_entry(workspace_id=completed_run.run.workspace_id, run_id=completed_run.id)
        )
    assert excinfo.value.code == "TRACE_RUN_MISSING"
    assert excinfo.value.retryable is False
    assert fake_langfuse_client.batches == []


async def test_handler_rejects_unsupported_kind(fake_langfuse_client) -> None:
    handler = LangfuseOutboxHandler(None, fake_langfuse_client)  # type: ignore[arg-type]
    with pytest.raises(OutboxError):
        await handler.handle(
            _make_entry(
                workspace_id=uuid.uuid4(), run_id=uuid.uuid4(), kind=OutboxKind.MEMORY_UPSERT
            )
        )
    assert fake_langfuse_client.batches == []
