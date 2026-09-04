"""
中断处理器 — 管理 Agent 执行的中断信号。

AbortHandler。
当用户取消操作或系统超时时，通过 AbortHandler 设置中断标志，
BaseAgent 在检查点检测到中断后停止执行。
"""

from __future__ import annotations


class AbortHandler:
    """中断处理器，管理中断标志和原因。

    用法:
        handler = AbortHandler()
        handler.abort("user_cancel")
        if handler.is_aborted:
            print(f"已中断: {handler.reason}")
    """

    def __init__(self) -> None:
        """初始化中断处理器，默认未中断。"""
        self._aborted: bool = False
        self._reason: str = ""

    def abort(self, reason: str = "unknown") -> None:
        """设置中断标志。

        Args:
            reason: 中断原因，如 "user_cancel"、"timeout"。
        """
        self._aborted = True
        self._reason = reason

    def reset(self) -> None:
        """重置中断标志，为新的 turn 做准备。"""
        self._aborted = False
        self._reason = ""

    @property
    def is_aborted(self) -> bool:
        """是否已中断。"""
        return self._aborted

    @property
    def reason(self) -> str:
        """中断原因。"""
        return self._reason