"""Token 估算(计划 02 Task 5 Step 3;架构文档 §7)。

Provider 无精确计数接口时使用保守估算:估算值只能偏高不能偏低,调用方在
此基础上再保留安全余量。测试可注入确定性估算器(如按字符计)替换启发式。
"""

from __future__ import annotations

from typing import Protocol


class TokenEstimator(Protocol):
    """文本 Token 估算协议(架构文档 §7:估算由注入的 TokenEstimator 完成)。"""

    def estimate(self, text: str) -> int:
        """估算一段文本的 Token 数;实现必须确定性且保守(不低估)。"""
        ...


class HeuristicTokenEstimator:
    """确定性启发式:约 4 字符 / Token,向上取整以保守估计。"""

    __slots__ = ("_chars_per_token",)

    def __init__(self, chars_per_token: int = 4) -> None:
        if chars_per_token < 1:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        return (len(text) + self._chars_per_token - 1) // self._chars_per_token


__all__ = ["HeuristicTokenEstimator", "TokenEstimator"]
