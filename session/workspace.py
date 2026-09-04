"""
Workspace — 工作空间，管理长期配置和 Source 列表。

Workspace 概念。
每个 Workspace 代表一个独立的电商运营场景（如一个品牌、一个店铺），
包含配置、Source 列表和权限模式默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Workspace:
    """工作空间，管理长期配置和 Source 列表。

    Attributes:
        workspace_id: 工作空间唯一标识。
        name: 工作空间名称。
        sources: 已附加的 Source 名称列表。
        rules: 业务规则声明列表（如 [{"type": "price_above_cost"}]），由权限管线执行。
        credentials: 凭据引用名列表（不存密钥本身，密钥在 CredentialStore）。
        created_at: 创建时间。
        metadata: 可扩展的元数据字典。
    """

    workspace_id: str
    name: str
    sources: list[str] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, str] = field(default_factory=dict)

    def add_source(self, source_name: str) -> None:
        """添加一个 Source 到工作空间。

        Args:
            source_name: Source 名称，如 "taobao"、"jd"、"douyin"。
        """
        if source_name not in self.sources:
            self.sources.append(source_name)

    def remove_source(self, source_name: str) -> None:
        """从工作空间移除一个 Source。

        Args:
            source_name: 要移除的 Source 名称。
        """
        if source_name in self.sources:
            self.sources.remove(source_name)

    def has_source(self, source_name: str) -> bool:
        """检查工作空间是否包含指定 Source。

        Args:
            source_name: Source 名称。

        Returns:
            bool: 是否包含该 Source。
        """
        return source_name in self.sources