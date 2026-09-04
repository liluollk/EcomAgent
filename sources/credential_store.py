"""
凭据存储 — Workspace 凭据引用的真实存取层。

设计原则（与描述"凭据引用"一致）：
- Workspace.credentials 只保存**引用名**（如 "taobao_oauth"），不保存密钥本身；
- 密钥本体由 CredentialStore 管理（内存存储，真实项目可替换为 OAuth 服务 / Vault）；
- 真实渠道接入时，注入点在 Source handler——调用前通过 resolve() 取凭据
  （见 README「接入真实渠道」）。

当前渠道为 Mock（无真实鉴权），默认预置三条演示凭据引用，用于打通
"引用 → 解析"链路；换真实渠道 = 替换 store 实现 + handler 内 resolve。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Credential:
    """一条凭据记录（引用解析结果）。"""

    name: str
    credential_type: str  # oauth_token / api_key / basic
    channel: str = ""
    masked_secret: str = ""  # 掩码值，仅用于展示；真实项目中此处为密钥引用
    extra: dict[str, Any] = field(default_factory=dict)


class CredentialStore:
    """凭据存储：注册、解析、列名。内存实现，接口面向真实凭证服务替换。"""

    def __init__(self) -> None:
        self._items: dict[str, Credential] = {}

    def register(self, credential: Credential) -> None:
        """注册一条凭据。"""
        self._items[credential.name] = credential

    def resolve(self, name: str) -> Optional[Credential]:
        """按引用名解析凭据（真实接入点在 Source handler / MCP server）。"""
        return self._items.get(name)

    def list_names(self) -> list[str]:
        """"列出全部已注册凭据的引用名。"""
        return list(self._items.keys())

    def has(self, name: str) -> bool:
        return name in self._items


def create_default_credential_store() -> CredentialStore:
    """预置三渠道演示凭据（Mock token，无真实密钥）。"""
    store = CredentialStore()
    for channel in ("taobao", "jd", "douyin"):
        store.register(Credential(
            name=f"{channel}_oauth",
            credential_type="oauth_token",
            channel=channel,
            masked_secret=f"mock-{channel}-token *****",
            extra={"scopes": ["inventory:read", "price:write"]},
        ))
    return store


# 全局默认凭据存储（与会话存储同生命周期；真实部署可注入外部实现）
DEFAULT_CREDENTIAL_STORE = create_default_credential_store()