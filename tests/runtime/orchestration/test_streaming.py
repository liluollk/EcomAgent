"""StreamingMessageRecorder 与 MessageStore 落库语义测试(计划 02 Task 5 Step 1/Step 4)。

内存替身(FakeMessageStore)验证合并/终态语义;真实落库用少量 DB 测试
(复用 ``tests/runtime/conftest.py`` 的 ``db_session`` / ``seeded_session`` /
``seeded_run``),验证只 flush 不 commit 与 messages 表状态。
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.backend.protocol import (
    BackendCompleted,
    BackendEvent,
    BackendFailed,
    BackendToolCall,
    CancellationToken,
    TextDelta,
)
from runtime.interfaces.state import MessageType
from runtime.orchestration.messages import MessageStore
from runtime.orchestration.streaming import StreamingMessageRecorder
from runtime.persistence.models.interaction import Message as MessageRow


class FakeMessageStore:
    """内存版 MessageStore:验证 recorder 的合并与终态语义,不触库。"""

    def __init__(self) -> None:
        self.content = ""
        self.status = "STREAMING"
        self.append_calls: list[str] = []
        self._message_id = uuid.uuid4()

    @property
    def id(self) -> uuid.UUID:
        return self._message_id

    async def start_assistant_message(
        self, *, session_id: uuid.UUID, run_id: uuid.UUID, sequence_no: int
    ) -> "FakeMessageStore":
        return self

    async def append_assistant_content(
        self, message: "FakeMessageStore", content_delta: str
    ) -> None:
        self.content += content_delta
        self.append_calls.append(content_delta)

    async def complete_assistant_message(self, message: "FakeMessageStore") -> None:
        self.status = "COMPLETED"

    async def abort_assistant_message(self, message: "FakeMessageStore") -> None:
        self.status = "ABORTED"


def _recorder(
    store: FakeMessageStore,
    merged: list[str] | None = None,
    threshold: int = 4,
) -> StreamingMessageRecorder:
    on_merged_text = None
    if merged is not None:

        async def _push(chunk: str) -> None:
            merged.append(chunk)

        on_merged_text = _push
    return StreamingMessageRecorder(
        store=store,
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        sequence_no=1,
        merge_threshold_chars=threshold,
        on_merged_text=on_merged_text,
    )


async def test_merged_text_callback_batches_deltas() -> None:
    store = FakeMessageStore()
    merged: list[str] = []
    recorder = _recorder(store, merged)

    async def events() -> AsyncIterator[BackendEvent]:
        for chunk in ("aaa", "bbb", "ccc", "d"):
            yield TextDelta(text_delta=chunk)
        yield BackendCompleted(finish_reason="stop")

    outcome = await recorder.consume(events())

    assert merged == ["aaabbb", "cccd"]
    assert store.append_calls == ["aaabbb", "cccd"]
    assert outcome.status == "COMPLETED"
    assert outcome.content == "aaabbbcccd"
    assert not outcome.error_code


async def test_stream_persists_and_checkpoints_before_requesting_next_chunk() -> None:
    store = FakeMessageStore()
    checkpoints: list[str] = []

    async def checkpoint() -> None:
        checkpoints.append("commit")

    recorder = StreamingMessageRecorder(
        store=store,
        workspace_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        sequence_no=1,
        merge_threshold_chars=4,
        checkpoint=checkpoint,
    )

    async def events() -> AsyncIterator[BackendEvent]:
        yield TextDelta(text_delta="four")
        assert checkpoints == ["commit", "commit"]
        yield BackendCompleted(finish_reason="stop")

    outcome = await recorder.consume(events())

    assert outcome.status == "COMPLETED"
    assert checkpoints == ["commit", "commit", "commit"]


async def test_cancellation_marks_message_aborted() -> None:
    store = FakeMessageStore()
    recorder = _recorder(store)
    cancellation = CancellationToken()

    async def events() -> AsyncIterator[BackendEvent]:
        yield TextDelta(text_delta="partial ")
        cancellation.cancel()
        yield TextDelta(text_delta="more")
        yield BackendCompleted(finish_reason="stop")

    outcome = await recorder.consume(events(), cancellation)

    assert outcome.status == "ABORTED"
    assert outcome.error_code == "CANCELLED"
    assert outcome.content == "partial "
    assert store.status == "ABORTED"


async def test_backend_failed_event_marks_aborted_with_error_code() -> None:
    store = FakeMessageStore()
    recorder = _recorder(store)

    async def events() -> AsyncIterator[BackendEvent]:
        yield TextDelta(text_delta="half answer")
        yield BackendFailed(error_code="PROVIDER_TIMEOUT", retryable=True)

    outcome = await recorder.consume(events())

    assert outcome.status == "ABORTED"
    assert outcome.error_code == "PROVIDER_TIMEOUT"
    assert outcome.content == "half answer"
    assert store.status == "ABORTED"


async def test_stream_without_completed_event_is_aborted() -> None:
    store = FakeMessageStore()
    recorder = _recorder(store)

    async def events() -> AsyncIterator[BackendEvent]:
        yield TextDelta(text_delta="x")

    outcome = await recorder.consume(events())

    assert outcome.status == "ABORTED"
    assert outcome.error_code == "STREAM_ENDED_WITHOUT_COMPLETION"
    assert outcome.content == "x"


async def test_streaming_failure_marks_assistant_message_aborted_in_db(
    db_session: AsyncSession, seeded_session, seeded_run
) -> None:
    store = MessageStore(session=db_session, workspace_id=seeded_session.workspace_id)
    recorder = StreamingMessageRecorder(
        store=store,
        workspace_id=seeded_session.workspace_id,
        session_id=seeded_session.id,
        run_id=seeded_run.id,
        sequence_no=1,
        merge_threshold_chars=4,
    )

    async def events() -> AsyncIterator[BackendEvent]:
        yield TextDelta(text_delta="partial secret answer")
        yield BackendFailed(error_code="PROVIDER_TIMEOUT", retryable=False)

    outcome = await recorder.consume(events())

    assert outcome.status == "ABORTED"
    row = await db_session.get(MessageRow, outcome.message_id)
    assert row is not None
    assert row.status.value == "ABORTED"
    assert row.content == "partial secret answer"
    assert row.completed_at is None


async def test_streaming_completion_persists_full_content_in_db(
    db_session: AsyncSession, seeded_session, seeded_run
) -> None:
    store = MessageStore(session=db_session, workspace_id=seeded_session.workspace_id)
    recorder = StreamingMessageRecorder(
        store=store,
        workspace_id=seeded_session.workspace_id,
        session_id=seeded_session.id,
        run_id=seeded_run.id,
        sequence_no=2,
        merge_threshold_chars=4,
    )

    async def events() -> AsyncIterator[BackendEvent]:
        for chunk in ("The ", "answer ", "is 42."):
            yield TextDelta(text_delta=chunk)
        yield BackendCompleted(finish_reason="stop")

    outcome = await recorder.consume(events())

    assert outcome.status == "COMPLETED"
    row = await db_session.get(MessageRow, outcome.message_id)
    assert row is not None
    assert row.role.value == "ASSISTANT"
    assert row.status.value == "COMPLETED"
    assert row.content == "The answer is 42."
    assert row.sequence_no == 2
    assert isinstance(row.completed_at, datetime)


async def test_store_list_completed_messages_excludes_aborted_assistant(
    db_session: AsyncSession, seeded_session
) -> None:
    store = MessageStore(session=db_session, workspace_id=seeded_session.workspace_id)
    await store.append_user_message(
        session_id=seeded_session.id, run_id=None, sequence_no=1, content="question"
    )
    completed = await store.start_assistant_message(
        session_id=seeded_session.id, run_id=None, sequence_no=2
    )
    await store.append_assistant_content(completed, "final answer")
    await store.complete_assistant_message(completed)
    aborted = await store.start_assistant_message(
        session_id=seeded_session.id, run_id=None, sequence_no=3
    )
    await store.append_assistant_content(aborted, "partial secret answer")
    await store.abort_assistant_message(aborted)
    await store.append_tool_message(
        session_id=seeded_session.id,
        run_id=None,
        sequence_no=4,
        call_id="call-1",
        tool_name="search_products",
        content="tool output",
    )

    messages = await store.list_completed_messages(session_id=seeded_session.id)

    assert [message.sequence_no for message in messages] == [1, 2, 4]
    assert all(message.content != "partial secret answer" for message in messages)
    assert messages[1].status is not None
    tool_message = messages[2]
    assert tool_message.role is MessageType.TOOL


async def test_store_restores_tool_calls_on_assistant_history(
    db_session: AsyncSession, seeded_session
) -> None:
    store = MessageStore(session=db_session, workspace_id=seeded_session.workspace_id)
    assistant = await store.start_assistant_message(
        session_id=seeded_session.id, run_id=None, sequence_no=1
    )
    await store.complete_assistant_message(assistant)
    tool_call = BackendToolCall(
        id="call-1", name="search_products", arguments={"query": "coffee"}
    )

    await store.set_assistant_tool_calls(assistant.id, (tool_call,))
    messages = await store.list_completed_messages(session_id=seeded_session.id)

    assert len(messages) == 1
    assert messages[0].tool_calls == (tool_call,)


async def test_store_persists_session_summary_provenance(
    db_session: AsyncSession, seeded_session
) -> None:
    store = MessageStore(session=db_session, workspace_id=seeded_session.workspace_id)
    row = await store.append_session_summary(
        session_id=seeded_session.id,
        run_id=None,
        sequence_no=5,
        covered_from_sequence=1,
        covered_to_sequence=3,
        summary_schema_version=1,
        source_hash="a" * 64,
        content='{"schema_version": 1, "summary": "digest", "key_facts": []}',
    )

    fetched = await db_session.get(MessageRow, row.id)
    assert fetched is not None
    assert fetched.role.value == "SESSION_SUMMARY"
    assert fetched.covered_from_sequence == 1
    assert fetched.covered_to_sequence == 3
    assert fetched.summary_schema_version == 1
    assert fetched.source_hash == "a" * 64
