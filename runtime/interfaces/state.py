"""Run 与 Message 的公共状态类型(架构文档 §6、§8)。

这里是这些枚举的**唯一定义处**。持久层(``runtime.persistence.base``)从本模块
导入并再导出,因此 ORM 读出的值与运行时代码引用的值是同一个枚举成员。

此前 ``RunStatus``、``MessageRole``、``MessageStatus`` 在本模块与持久层各定义了
一份,两套 ``str`` 枚举 ``==`` 成立但 ``is`` 不成立。调用方只要忘了 ``.value``
重新构造,``is`` 判断就会永远为假,而且是静默的:

- ``_conversation_view`` 认不出最后一条是用户消息,当前消息混进历史;
- ``context.py`` 认不出 ``SESSION_SUMMARY``,摘要裁剪整段失效。

``MessageType`` / ``AssistantMessageStatus`` 保留为别名,指向同一组成员值,
避免为改名而改动大量调用点。
"""

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


class MessageRole(str, Enum):
    """Message 角色(架构文档 §6)。SESSION_SUMMARY 是 Runtime 内部角色。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"
    SESSION_SUMMARY = "SESSION_SUMMARY"


class MessageStatus(str, Enum):
    """Message 状态(数据模型文档 §5)。只有 COMPLETED 的 Assistant Message
    可以进入后续 Context。
    """

    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


MessageType = MessageRole
"""兼容别名:角色语义与 :class:`MessageRole` 完全相同,是同一个枚举对象。"""

AssistantMessageStatus = MessageStatus
"""兼容别名:助手消息状态与 :class:`MessageStatus` 相同,是同一个枚举对象。"""


__all__ = [
    "AssistantMessageStatus",
    "MessageRole",
    "MessageStatus",
    "MessageType",
    "RunStatus",
]
