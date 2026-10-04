"""确定性故障注入器(计划 03 Task 5;架构文档 §7)。

Mock 平台用命名故障模拟真实平台异常——写前超时、提交后响应丢失、回查
暂时不确定与持续失败——用于验证"写入已生效但响应丢失"等恢复场景。
不使用随机概率:故障状态只由 ``inject``/``clear`` 显式控制,同一故障脚本
在相同输入上必然产生相同结果。
"""

from typing import Final


TIMEOUT_BEFORE_WRITE: Final = "timeout_before_write"
RESPONSE_LOST_AFTER_COMMIT: Final = "response_lost_after_commit"
TEMPORARY_RECONCILE_UNKNOWN: Final = "temporary_reconcile_unknown"
PERMANENT_FAILURE: Final = "permanent_failure"

KNOWN_FAULTS: Final[frozenset[str]] = frozenset(
    {
        TIMEOUT_BEFORE_WRITE,
        RESPONSE_LOST_AFTER_COMMIT,
        TEMPORARY_RECONCILE_UNKNOWN,
        PERMANENT_FAILURE,
    }
)


class FaultInjector:
    """命名故障注入;激活状态由测试显式管理,无随机触发。

    - ``timeout_before_write``/``response_lost_after_commit``/
      ``permanent_failure``:持续生效直到 ``clear``;
    - ``temporary_reconcile_unknown``:按语义一次性生效,注入后首次
      ``reconcile`` 返回 UNKNOWN,其后恢复真实结论。
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._reconcile_unknown_remaining: int = 0

    def inject(self, name: str) -> None:
        """激活命名故障;未知名称立即报错(确定性,防止拼写漂移)。"""
        if name not in KNOWN_FAULTS:
            raise ValueError(f"unknown fault: {name!r}")
        if name == TEMPORARY_RECONCILE_UNKNOWN:
            self._reconcile_unknown_remaining = 1
        else:
            self._active.add(name)

    def clear(self, name: str | None = None) -> None:
        """清除指定故障;缺省清除全部。"""
        if name is None:
            self._active.clear()
            self._reconcile_unknown_remaining = 0
            return
        if name == TEMPORARY_RECONCILE_UNKNOWN:
            self._reconcile_unknown_remaining = 0
        else:
            self._active.discard(name)

    def should_timeout_before_write(self) -> bool:
        """写入路径尚未开始即超时(请求未达平台逻辑)。"""
        return TIMEOUT_BEFORE_WRITE in self._active

    def should_lose_response_after_commit(self) -> bool:
        """写入已提交,但响应未返回调用方。"""
        return RESPONSE_LOST_AFTER_COMMIT in self._active

    def should_fail_permanently(self) -> bool:
        """平台逻辑持续不可用(确定失败,永不返回 UNKNOWN)。"""
        return PERMANENT_FAILURE in self._active

    def consume_reconcile_unknown(self) -> bool:
        """临时回查不确定是否生效;生效则消耗一次配额。"""
        if self._reconcile_unknown_remaining > 0:
            self._reconcile_unknown_remaining -= 1
            return True
        return False


__all__ = [
    "KNOWN_FAULTS",
    "PERMANENT_FAILURE",
    "RESPONSE_LOST_AFTER_COMMIT",
    "TEMPORARY_RECONCILE_UNKNOWN",
    "TIMEOUT_BEFORE_WRITE",
    "FaultInjector",
]
