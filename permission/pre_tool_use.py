"""
PreToolUse 管线 — 工具调用前的安全检查器链。

runPreToolUseChecks 机制。
在工具实际执行前，通过可插拔的检查器链进行安全校验。
检查器可以返回 ALLOW（放行）、BLOCK（拦截）、ASK（询问用户）。

与权限模式的关系：
- READONLY 模式：所有写操作被 PreToolUse 管线自动 BLOCK。
- ASK 模式：写操作触发 ASK，读操作 ALLOW。
- EXECUTE 模式：所有操作 ALLOW（除非被业务规则主动 BLOCK）。
"""

from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class PreToolUseAction(Enum):
    """PreToolUse 检查器返回的动作。

    ALLOW: 允许执行工具调用。
    BLOCK: 拦截工具调用，向用户返回拦截原因。
    ASK: 请求用户确认后再执行。
    """

    ALLOW = auto()
    BLOCK = auto()
    ASK = auto()


@dataclass
class PreToolUseResult:
    """PreToolUse 检查器的返回结果。

    Attributes:
        action: 检查器决策（ALLOW/BLOCK/ASK）。
        reason: 决策原因，在 BLOCK 或 ASK 时必填。
    """

    action: PreToolUseAction
    reason: str = ""


class PreToolUsePipeline:
    """可插拔的 PreToolUse 检查器链。

    检查器按注册顺序依次执行，实现短路逻辑：
    - 任一检查器返回 BLOCK → 立即返回 BLOCK。
    - 任一检查器返回 ASK → 立即返回 ASK（不执行后续检查器）。
    - 所有检查器返回 ALLOW → 最终返回 ALLOW。

    用法:
        pipeline = PreToolUsePipeline()
        pipeline.add_checker(my_custom_checker)
        result = pipeline.check("update_price", {"sku": "BH-201"})
    """

    def __init__(self) -> None:
        """初始化空的检查器链。"""
        self._checkers: list[Callable[..., PreToolUseResult]] = []

    def add_checker(self, checker: Callable[..., PreToolUseResult]) -> None:
        """向管线尾部添加一个检查器。

        Args:
            checker: 检查器函数，签名应为 (tool_name, tool_input) -> PreToolUseResult。
        """
        self._checkers.append(checker)

    def remove_checker(self, checker: Callable[..., PreToolUseResult]) -> None:
        """从管线中移除一个检查器。

        Args:
            checker: 要移除的检查器函数。
        """
        if checker in self._checkers:
            self._checkers.remove(checker)

    def check(self, tool_name: str, tool_input: dict[str, Any]) -> PreToolUseResult:
        """按顺序执行所有检查器，实现短路逻辑。

        Args:
            tool_name: 工具名称。
            tool_input: 工具参数。

        Returns:
            PreToolUseResult: 第一个非 ALLOW 的结果，或最终 ALLOW。
        """
        for checker in self._checkers:
            try:
                result = checker(tool_name, tool_input)
            except Exception:
                result = PreToolUseResult(
                    action=PreToolUseAction.BLOCK,
                    reason="检查器执行异常，已拦截",
                )
            if result.action != PreToolUseAction.ALLOW:
                return result
        return PreToolUseResult(action=PreToolUseAction.ALLOW)