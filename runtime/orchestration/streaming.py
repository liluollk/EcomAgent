"""StreamingMessageRecorder:消费 BackendEvent 流并维护 Assistant Message 生命周期
(计划 02 Task 5;架构文档 §6)。

合并推送设计(不逐 token 落库):

- TextDelta 先在内存缓冲累积,达到 ``merge_threshold_chars`` 才一次性
  ``MessageStore.append_assistant_content`` 落库,并触发 ``on_merged_text``
  回调——AgentEvent 推送与 Message 内容更新都发生在合并边界;
- ``BackendCompleted`` → 剩余缓冲落库 + COMPLETED(可进入后续 Context);
- ``BackendFailed`` / 取消 / 流意外终止(无 Completed 事件)→ 已收内容落库
  (仅供诊断)+ ABORTED;
- 异常冒泡时同样先标记 ABORTED 再抛出,保证半截流不会以 COMPLETED 存留;
- ``ToolCallCompleted`` 原样收集,交由上层(PreToolUse 管线,Task 6/8)处理。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from runtime.backend.protocol import (
    BackendCompleted,
    BackendEvent,
    BackendFailed,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)
from runtime.orchestration.messages import MessageStore
from runtime.persistence.models.interaction import Message


@dataclass(frozen=True)
class StreamingOutcome:
    """一次流式消费的终态;``error_code`` 仅在 ABORTED 时非空。"""

    message_id: uuid.UUID
    status: Literal["COMPLETED", "ABORTED"]
    content: str
    tool_calls: tuple[ToolCallCompleted, ...] = ()
    error_code: str | None = None


class StreamingMessageRecorder:
    """消费标准化 BackendEvent 流;终态(COMPLETED/ABORTED)落库只 flush 不 commit。"""

    def __init__(
        self,
        *,
        store: MessageStore,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        sequence_no: int,
        merge_threshold_chars: int = 256,
        on_merged_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        if merge_threshold_chars < 1:
            raise ValueError("merge_threshold_chars must be positive")
        self._store = store
        self._workspace_id = workspace_id
        self._session_id = session_id
        self._run_id = run_id
        self._sequence_no = sequence_no
        self._merge_threshold_chars = merge_threshold_chars
        self._on_merged_text = on_merged_text

    async def consume(
        self,
        events: AsyncIterator[BackendEvent],
        cancellation: CancellationToken | None = None,
    ) -> StreamingOutcome:
        """流式累积内容并在安全点合并落库;终态决定 COMPLETED / ABORTED。"""
        message = await self._store.start_assistant_message(
            session_id=self._session_id,
            run_id=self._run_id,
            sequence_no=self._sequence_no,
        )
        content: list[str] = []
        pending: list[str] = []
        pending_len = 0
        tool_calls: list[ToolCallCompleted] = []
        error_code: str | None = None
        completed = False

        try:
            async for event in events:
                if cancellation is not None and cancellation.is_cancelled:
                    error_code = "CANCELLED"
                    break
                if isinstance(event, TextDelta):
                    content.append(event.text_delta)
                    pending.append(event.text_delta)
                    pending_len += len(event.text_delta)
                    if pending_len >= self._merge_threshold_chars:
                        await self._flush(message, pending)
                        pending_len = 0
                elif isinstance(event, ToolCallCompleted):
                    tool_calls.append(event)
                elif isinstance(event, BackendCompleted):
                    completed = True
                    break
                elif isinstance(event, BackendFailed):
                    error_code = event.error_code
                    break
        except Exception:
            await self._flush(message, pending)
            await self._store.abort_assistant_message(message)
            raise

        await self._flush(message, pending)
        if completed and error_code is None:
            await self._store.complete_assistant_message(message)
            return StreamingOutcome(
                message_id=message.id,
                status="COMPLETED",
                content="".join(content),
                tool_calls=tuple(tool_calls),
            )
        await self._store.abort_assistant_message(message)
        if error_code is None:
            error_code = "STREAM_ENDED_WITHOUT_COMPLETION"
        return StreamingOutcome(
            message_id=message.id,
            status="ABORTED",
            content="".join(content),
            tool_calls=tuple(tool_calls),
            error_code=error_code,
        )

    async def _flush(self, message: Message, pending: list[str]) -> None:
        """把累积缓冲作为一次合并块落库并触发合并事件回调。"""
        chunk = "".join(pending)
        pending.clear()
        if not chunk:
            return
        await self._store.append_assistant_content(message, chunk)
        if self._on_merged_text is not None:
            await self._on_merged_text(chunk)


__all__ = ["StreamingMessageRecorder", "StreamingOutcome"]
