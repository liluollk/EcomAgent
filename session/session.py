"""
Session — 会话层，隔离单次任务上下文。

Session 概念。
Session 是 Workspace 下的子状态容器，管理单次对话的消息历史、
工具调用记录、权限请求记录和权限模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto

from .workspace import Workspace


class PermissionMode(Enum):
    """权限模式枚举 — 三种会话权限模式：READONLY / ASK / EXECUTE。

    READONLY: 只读模式，仅允许查询类操作，写操作自动拒绝。
    ASK: 询问模式，每次工具调用前请求用户确认（默认模式）。
    EXECUTE: 执行模式，自动执行所有工具调用，适合管理员。
    """

    READONLY = auto()
    ASK = auto()
    EXECUTE = auto()


class SessionStatus(Enum):
    """会话状态枚举。

    ACTIVE: 活跃状态，可以接收新消息。
    COMPLETED: 已完成状态，不再接受新消息。
    ABORTED: 已中断状态，用户主动终止。
    """

    ACTIVE = auto()
    COMPLETED = auto()
    ABORTED = auto()


class ExecutionState(Enum):
    """执行状态枚举 — 单次 turn 的瞬态执行进度。

    IDLE: 无执行中任务，可接收新消息。
    RUNNING: 正在执行 turn（委托后端 / 工具循环）。
    WAITING_PERMISSION: 权限确认挂起中（ASK 模式阻塞等待）。
    COMPLETED: 本次 turn 执行完成。
    ABORTED: 本次执行被中断。
    """

    IDLE = auto()
    RUNNING = auto()
    WAITING_PERMISSION = auto()
    COMPLETED = auto()
    ABORTED = auto()


@dataclass
class Session:
    """会话，隔离单次任务的上下文状态。

    Attributes:
        session_id: 会话唯一标识。
        workspace: 所属工作空间引用。
        permission_mode: 当前权限模式，默认 ASK。
        status: 会话状态，默认 ACTIVE。
        created_at: 创建时间。
        active_sources: 当前会话激活的 Source 列表。
        tool_calls: 工具调用历史记录。
        permission_requests: 权限请求历史记录。
        messages: 消息历史（对话记录）。
        _saved_count: 已持久化的消息数量，用于增量写入。
    """

    session_id: str
    workspace: Workspace
    permission_mode: PermissionMode = PermissionMode.ASK
    status: SessionStatus = SessionStatus.ACTIVE
    execution_state: ExecutionState = ExecutionState.IDLE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    active_sources: list[str] = field(default_factory=list)
    user: dict = field(default_factory=lambda: {"user_id": "unknown", "role": "operator"})
    model_state: dict = field(default_factory=lambda: {"provider": "openai", "model": "gpt-4o-mini"})
    tool_calls: list[dict] = field(default_factory=list)
    permission_requests: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    _saved_count: int = 0
    _saved_tool_calls: int = 0
    _saved_permission_requests: int = 0

    def add_message(self, role: str, content: str) -> None:
        """向会话消息历史追加一条消息。

        Args:
            role: 消息角色，如 "user"、"assistant"、"system"。
            content: 消息内容。
        """
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_message_record(self, message: dict) -> None:
        """追加一条带结构化字段的消息记录（时间戳由调用方补齐）。

        用于 assistant(tool_calls) 与 role="tool" 等非纯文本消息，
        保证工具调用链完整进入持久化与恢复路径。

        Args:
            message: 完整消息字典，必须包含 role / content / timestamp。
        """
        self.messages.append(message)

    def add_tool_call(self, tool_call: dict) -> None:
        """记录一次工具调用。

        Args:
            tool_call: 工具调用信息字典，包含 tool_name、tool_use_id、input、result。
        """
        self.tool_calls.append(tool_call)

    def get_unsaved_messages(self) -> list[dict]:
        """获取自上次持久化以来的新消息列表。

        Returns:
            list[dict]: 未持久化的消息列表。
        """
        return self.messages[self._saved_count:]

    def get_unsaved_tool_calls(self) -> list[dict]:
        """获取自上次持久化以来的新工具调用记录。

        Returns:
            list[dict]: 未持久化的工具调用列表。
        """
        return self.tool_calls[self._saved_tool_calls:]

    def get_unsaved_permission_requests(self) -> list[dict]:
        """获取自上次持久化以来的新权限请求记录。

        Returns:
            list[dict]: 未持久化的权限请求列表。
        """
        return self.permission_requests[self._saved_permission_requests:]

    def mark_saved(self) -> None:
        """标记所有消息与审计记录已持久化，更新各类增量计数。"""
        self._saved_count = len(self.messages)
        self._saved_tool_calls = len(self.tool_calls)
        self._saved_permission_requests = len(self.permission_requests)

    def abort(self) -> None:
        """中断会话，将状态设为 ABORTED。"""
        self.status = SessionStatus.ABORTED

    def complete(self) -> None:
        """完成会话，将状态设为 COMPLETED。"""
        self.status = SessionStatus.COMPLETED