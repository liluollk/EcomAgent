"""
权限存储 — 记住用户对权限请求的决策，支持 TTL 过期。

承载「允许本次操作 / 接下来的 X 分钟不再询问」的决策记忆。
存储格式为 key → (value, expiry_timestamp)，支持按 key 查询和自动过期。
"""

from __future__ import annotations

import time
from typing import Optional


class PermissionStore:
    """权限决策存储，支持 TTL 过期。

    用于实现 "接下来的 X 分钟不再询问" 的快捷操作。
    存储 key 格式示例: "tool:update_price:SKU-001"。

    用法:
        store = PermissionStore()
        store.remember("tool:update_price:SKU-001", ttl_minutes=30)
        if store.is_remembered("tool:update_price:SKU-001"):
            skip_confirmation()
    """

    def __init__(self) -> None:
        """初始化空的权限存储。"""
        self._store: dict[str, tuple[str, float]] = {}

    def remember(self, key: str, ttl_minutes: int = 30) -> None:
        """记住一个权限决策，设定 TTL 过期时间。

        Args:
            key: 权限决策的唯一标识。
            ttl_minutes: 过期时间（分钟），默认 30 分钟。
        """
        expiry = time.time() + ttl_minutes * 60
        self._store[key] = ("allowed", expiry)

    def is_remembered(self, key: str) -> bool:
        """检查指定 key 是否仍在有效期内。

        如果 key 已过期，自动清理。

        Args:
            key: 权限决策的唯一标识。

        Returns:
            bool: 是否在有效期内。
        """
        if key not in self._store:
            return False
        _, expiry = self._store[key]
        if time.time() > expiry:
            del self._store[key]
            return False
        return True

    def forget(self, key: str) -> None:
        """手动移除一个权限决策。

        Args:
            key: 权限决策的唯一标识。
        """
        self._store.pop(key, None)

    def forget_all(self) -> None:
        """清空所有权限决策。"""
        self._store.clear()