"""记忆生命周期状态机与 Revision 规则(计划 04 Task 7;数据模型文档 §11)。

状态迁移:
- ``ACTIVE`` → ``SUPERSEDED``(被纠正替代)、``EXPIRED``(到期)、
  ``DELETED``(Tombstone);
- ``SUPERSEDED`` / ``EXPIRED`` / ``DELETED`` 为终态,不可再迁移。

Revision 规则:新记忆从 ``INITIAL_REVISION`` 开始;纠正创建新记录,
``revision = 旧记录 revision + 1``、``supersedes_id = 旧记录 id``,旧记录
置 ``SUPERSEDED``。检索过滤只接受 ``ACTIVE`` 且未过期的记录。
"""

from typing import Final

from runtime.persistence.base import MemoryLifecycleStatus


MemoryLifecycle = MemoryLifecycleStatus
"""领域别名:生命周期状态枚举(与持久层枚举同源,禁止第二份定义)。"""

INITIAL_REVISION: Final = 1
"""新记忆的起始 Revision。"""

ALLOWED_TRANSITIONS: Final[dict[MemoryLifecycleStatus, frozenset[MemoryLifecycleStatus]]] = {
    MemoryLifecycleStatus.ACTIVE: frozenset(
        {
            MemoryLifecycleStatus.SUPERSEDED,
            MemoryLifecycleStatus.EXPIRED,
            MemoryLifecycleStatus.DELETED,
        }
    ),
    MemoryLifecycleStatus.SUPERSEDED: frozenset(),
    MemoryLifecycleStatus.EXPIRED: frozenset(),
    MemoryLifecycleStatus.DELETED: frozenset(),
}
"""受控状态迁移表(数据模型文档 §11):终态不可迁移。"""

RETRIEVABLE_STATUSES: Final[frozenset[MemoryLifecycleStatus]] = frozenset(
    {MemoryLifecycleStatus.ACTIVE}
)
"""允许进入 Context 检索结果的生命周期状态。"""


class MemoryLifecycleError(RuntimeError):
    """违反生命周期状态机的迁移;携带稳定状态码/错误码供 API 翻译。"""

    status_code = 409
    code = "CONFLICT"


def can_transition(current: MemoryLifecycleStatus, target: MemoryLifecycleStatus) -> bool:
    """判断 ``current → target`` 是否为受控迁移。"""
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: MemoryLifecycleStatus, target: MemoryLifecycleStatus) -> None:
    """校验 ``current → target``;非法迁移抛 :class:`MemoryLifecycleError`。"""
    if not can_transition(current, target):
        raise MemoryLifecycleError(
            f"memory lifecycle {current.value} cannot transition to {target.value}"
        )


def next_revision(current: int) -> int:
    """纠正产生的新 Revision:旧 Revision 加一。"""
    return current + 1


def is_retrievable(status: MemoryLifecycleStatus) -> bool:
    """生命周期是否允许进入检索结果(仅 ACTIVE)。"""
    return status in RETRIEVABLE_STATUSES


__all__ = [
    "ALLOWED_TRANSITIONS",
    "INITIAL_REVISION",
    "RETRIEVABLE_STATUSES",
    "MemoryLifecycle",
    "MemoryLifecycleError",
    "MemoryLifecycleStatus",
    "assert_transition",
    "can_transition",
    "is_retrievable",
    "next_revision",
]
