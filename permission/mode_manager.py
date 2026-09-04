"""
权限模式管理器 — 管理 readonly/ask/execute 三种权限模式的状态切换。

ModeManager。
三模式定义：
- READONLY: 仅允许只读操作，危险操作（写/删）自动拒绝。
- ASK: 每次工具调用前请求用户确认（默认模式）。
- EXECUTE: 自动执行所有工具调用，不询问用户。
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Optional


class PermissionMode(Enum):
    """权限模式枚举。

    READONLY: 只读模式，仅允许只读操作。
    ASK: 询问模式，危险操作需用户确认。
    EXECUTE: 执行模式，自动执行所有操作。
    """

    READONLY = auto()
    ASK = auto()
    EXECUTE = auto()


class ModeManager:
    """权限模式管理器，管理当前模式状态和切换逻辑。

    用法:
        manager = ModeManager(PermissionMode.ASK)
        manager.set_mode(PermissionMode.EXECUTE)
        if manager.is_write_allowed():
            perform_write()
    """

    def __init__(self, default_mode: PermissionMode = PermissionMode.ASK) -> None:
        """初始化模式管理器。

        Args:
            default_mode: 默认权限模式，通常为 ASK。
        """
        self._mode: PermissionMode = default_mode

    @property
    def mode(self) -> PermissionMode:
        """获取当前权限模式。"""
        return self._mode

    def set_mode(self, mode: PermissionMode) -> None:
        """设置新的权限模式。

        Args:
            mode: 新的权限模式。
        """
        self._mode = mode

    def is_write_allowed(self) -> bool:
        """检查当前模式是否允许写操作。

        READONLY 模式下禁止写操作，ASK 和 EXECUTE 模式下允许。

        Returns:
            bool: 是否允许写操作。
        """
        return self._mode != PermissionMode.READONLY

    def is_confirmation_required(self) -> bool:
        """检查当前模式是否需要用户确认工具调用。

        ASK 模式下需要确认，READONLY 和 EXECUTE 模式下不需要。

        Returns:
            bool: 是否需要确认。
        """
        return self._mode == PermissionMode.ASK

    def is_auto_execute(self) -> bool:
        """检查当前模式是否自动执行所有工具调用。

        EXECUTE 模式下自动执行，READONLY 和 ASK 模式下不自动执行。

        Returns:
            bool: 是否自动执行。
        """
        return self._mode == PermissionMode.EXECUTE