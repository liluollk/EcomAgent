"""Run 与 Message 的公共状态类型(架构文档 §6、§8)。"""

from enum import Enum


class RunStatus(str, Enum):
    """Run 生命周期状态(计划 01 Task 2)。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class MessageType(str, Enum):
    """Message 类型(架构文档 §6)。SESSION_SUMMARY 是 Runtime 内部类型。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"
    SESSION_SUMMARY = "SESSION_SUMMARY"


class AssistantMessageStatus(str, Enum):
    """Assistant Message 状态(架构文档 §6)。

    只有 COMPLETED 的 Assistant Message 可以进入后续 Context。
    """

    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
