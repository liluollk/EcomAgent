"""MessageStore:Message 生命周期与持久化服务(计划 02 Task 5;架构文档 §6)。

- Assistant Message 以 STREAMING 创建,流式内容按合并块累积追加(不逐 token 落库);
- 只有 COMPLETED 的 Message 可进入后续 Context(:meth:`list_completed_messages` 过滤);
- Provider 失败 / 取消 / Worker 丢失 → ABORTED,已收内容仅供诊断(§6);
- SESSION_SUMMARY 行必须同时携带覆盖区间、Schema 版本与来源 Hash
  (messages 表 ck_messages_summary_coverage 约束);
- 本服务只 flush 不 commit;事务边界由调用方(UnitOfWork)持有。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.backend.protocol import BackendToolCall
from runtime.interfaces.state import AssistantMessageStatus, MessageType
from runtime.orchestration.context import ConversationMessage
from runtime.persistence.base import MessageRole, MessageStatus
from runtime.persistence.models.interaction import Message


class MessageStore:
    """绑定 AsyncSession 与 workspace 上下文的 Message 服务(只 flush 不 commit)。"""

    def __init__(self, *, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    async def start_assistant_message(
        self, *, session_id: uuid.UUID, run_id: uuid.UUID | None, sequence_no: int
    ) -> Message:
        """创建 STREAMING 状态的 Assistant Message;内容由流式块累积填充。"""
        row = Message(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=run_id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.STREAMING,
            content="",
            sequence_no=sequence_no,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def append_assistant_content(self, message: Message, content_delta: str) -> None:
        """按合并块追加流式内容(架构文档 §6:不要求每个 token 单独落库)。"""
        if message.status is not MessageStatus.STREAMING:
            raise ValueError("assistant message is not streaming")
        message.content += content_delta
        await self._session.flush()

    async def complete_assistant_message(self, message: Message) -> None:
        """流正常完成:COMPLETED 后才可进入后续 Context。"""
        if message.status is not MessageStatus.STREAMING:
            raise ValueError("assistant message is not streaming")
        message.status = MessageStatus.COMPLETED
        message.completed_at = datetime.now(UTC)
        await self._session.flush()

    async def abort_assistant_message(self, message: Message) -> None:
        """Provider 失败/取消/Worker 丢失:标记 ABORTED,内容仅供诊断。"""
        message.status = MessageStatus.ABORTED
        await self._session.flush()

    async def append_user_message(
        self,
        *,
        session_id: uuid.UUID,
        run_id: uuid.UUID | None,
        sequence_no: int,
        content: str,
    ) -> Message:
        """用户消息在创建 Run 前已完整持久化(架构文档 §6)。"""
        row = Message(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=run_id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETED,
            content=content,
            sequence_no=sequence_no,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def append_tool_message(
        self,
        *,
        session_id: uuid.UUID,
        run_id: uuid.UUID | None,
        sequence_no: int,
        call_id: str | None,
        tool_name: str | None,
        content: str,
    ) -> Message:
        """ToolResult 以 TOOL Message 回填;call_id/tool_name 记入 structured_content。"""
        row = Message(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=run_id,
            role=MessageRole.TOOL,
            status=MessageStatus.COMPLETED,
            content=content,
            sequence_no=sequence_no,
            structured_content={"call_id": call_id, "tool_name": tool_name},
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def set_assistant_tool_calls(
        self, message_id: uuid.UUID, tool_calls: tuple[BackendToolCall, ...]
    ) -> None:
        """把模型原始函数调用保存在 Assistant Message 上,用于恢复合法对话历史。"""
        row = await self._session.get(Message, message_id)
        if row is None or row.workspace_id != self._workspace_id:
            raise LookupError(f"assistant message {message_id} not found")
        if row.role is not MessageRole.ASSISTANT or row.status is not MessageStatus.COMPLETED:
            raise ValueError("tool calls can only be attached to a completed assistant message")
        row.structured_content = {
            "tool_calls": [tool_call.model_dump(mode="json") for tool_call in tool_calls]
        }
        await self._session.flush()

    async def append_session_summary(
        self,
        *,
        session_id: uuid.UUID,
        run_id: uuid.UUID | None,
        sequence_no: int,
        covered_from_sequence: int,
        covered_to_sequence: int,
        summary_schema_version: int,
        source_hash: str,
        content: str,
    ) -> Message:
        """持久化 SESSION_SUMMARY;四项出处字段由表 CHECK 约束保证一致。"""
        row = Message(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=run_id,
            role=MessageRole.SESSION_SUMMARY,
            status=MessageStatus.COMPLETED,
            content=content,
            sequence_no=sequence_no,
            covered_from_sequence=covered_from_sequence,
            covered_to_sequence=covered_to_sequence,
            summary_schema_version=summary_schema_version,
            source_hash=source_hash,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_completed_messages(
        self, *, session_id: uuid.UUID, up_to_sequence: int | None = None
    ) -> tuple[ConversationMessage, ...]:
        """读取可进入后续 Context 的消息:Assistant 仅 COMPLETED(§6),按序返回。"""
        stmt = (
            select(Message)
            .where(
                Message.workspace_id == self._workspace_id,
                Message.session_id == session_id,
                or_(
                    Message.role != MessageRole.ASSISTANT,
                    Message.status == MessageStatus.COMPLETED,
                ),
            )
            .order_by(Message.sequence_no.asc())
        )
        if up_to_sequence is not None:
            stmt = stmt.where(Message.sequence_no <= up_to_sequence)
        rows = (await self._session.scalars(stmt)).all()
        return tuple(self._to_conversation_message(row) for row in rows)

    @staticmethod
    def _to_conversation_message(row: Message) -> ConversationMessage:
        status = (
            AssistantMessageStatus(row.status.value)
            if row.role is MessageRole.ASSISTANT
            else None
        )
        return ConversationMessage(
            sequence_no=row.sequence_no,
            role=MessageType(row.role.value),
            content=row.content,
            status=status,
            call_id=(row.structured_content or {}).get("call_id")
            if row.role is MessageRole.TOOL
            else None,
            name=(row.structured_content or {}).get("tool_name")
            if row.role is MessageRole.TOOL
            else None,
            covered_from_sequence=row.covered_from_sequence,
            covered_to_sequence=row.covered_to_sequence,
            summary_schema_version=row.summary_schema_version,
            source_hash=row.source_hash,
            tool_calls=tuple(
                BackendToolCall.model_validate(tool_call)
                for tool_call in (row.structured_content or {}).get("tool_calls", [])
            )
            if row.role is MessageRole.ASSISTANT
            else (),
        )


__all__ = ["MessageStore"]
