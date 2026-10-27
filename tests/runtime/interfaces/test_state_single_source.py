"""状态枚举必须只有一份定义。

``runtime.interfaces.state`` 与 ``runtime.persistence.base`` 曾经各自定义
``RunStatus`` / ``MessageRole`` / ``MessageStatus``。两套 ``str`` 枚举
``==`` 成立而 ``is`` 不成立,于是"从 ORM 读出的值"和"代码里的常量"用 ``is``
比较时永远为假——静默、且类型检查发现不了:

- 会话装配认不出最后一条是用户消息,当前消息被混进历史;
- 上下文裁剪认不出 ``SESSION_SUMMARY``,摘要逻辑整段失效。

持久层现在从接口层导入再导出。这些断言防止定义再次分叉。
"""

from __future__ import annotations

from runtime.interfaces.state import AssistantMessageStatus, MessageType, RunStatus
from runtime.persistence.base import MessageRole, MessageStatus
from runtime.persistence.base import RunStatus as PersistenceRunStatus


def test_run_status_has_single_definition() -> None:
    assert RunStatus is PersistenceRunStatus


def test_run_status_members_are_identical() -> None:
    for member in RunStatus:
        assert member is PersistenceRunStatus(member.value)


def test_message_type_is_message_role() -> None:
    """别名必须指向同一个枚举对象,否则 ``is`` 比较会再次失效。"""
    assert MessageType is MessageRole
    assert MessageType.SESSION_SUMMARY is MessageRole.SESSION_SUMMARY


def test_assistant_message_status_is_message_status() -> None:
    assert AssistantMessageStatus is MessageStatus
    assert AssistantMessageStatus.COMPLETED is MessageStatus.COMPLETED


def test_orm_role_matches_interface_constant() -> None:
    """模拟 ORM 回填后最常见的比较:role 来自列类型,常量来自接口层。"""
    stored = MessageRole.USER
    assert stored is MessageType.USER
