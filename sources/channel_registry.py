"""ChannelRegistry — 渠道配置化注册表（接入真实渠道的配置化骨架）。

目标：让「拥有平台资质后，在设置里新增渠道」成为可操作、可持久化、即时起效的功能。

设计：
- 渠道配置持久化到 JSON 文件（默认 data/channels.json，可用 CHANNEL_CONFIG_FILE 覆盖）。
  主进程管理 API 写入，MCP Server 子进程与 mock 平台服务共享读取同一文件，实现跨进程同步。
- 每个渠道一个独立 REST client（base_url + 鉴权头），按渠道名解析；新增渠道无需新增
  工具（11 个工具按 channel 参数分派），写入配置后下次调用即生效，无需重启。
- 鉴权：auth_type=mock 用 mock key；auth_type=api_key 用配置的 api_key（或回退 mock key）。
  真实 OAuth token 换取/签名由各平台 adapter 负责（本层只负责注入鉴权头发送）。

边界（诚实声明）：
- 协议/装配/鉴权配置化：新增渠道 = 填配置即发 HTTP 到目标 base_url。
- 各真实平台字段映射/签名差异仍需每渠道一个 adapter，本层不代写。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from integrations.commerce.client import ChannelRestClient, DEFAULT_PLATFORM_URL
from integrations.commerce.adapter import (
    PLATFORM_KINDS,
    get_adapter,
    platform_is_real,
)

# mock 平台模拟鉴权 key（真实平台：各渠道 OAuth token）
MOCK_API_KEY = "mock-channel-key"

# 键仅 mask 展示，不落明文返回给前端
_SENSITIVE = {"api_key", "app_secret", "access_token", "app_key"}


# 默认三渠道配置（base_url 为空 = 指向本地 mock 渠道平台服务）
_DEFAULT_CHANNELS: list[dict[str, Any]] = [
    {
        "name": "taobao",
        "label": "淘宝",
        "base_url": "",
        "platform": "mock",
        "auth_type": "mock",
        "enabled": True,
    },
    {
        "name": "jd",
        "label": "京东",
        "base_url": "",
        "platform": "mock",
        "auth_type": "mock",
        "enabled": True,
    },
    {
        "name": "douyin",
        "label": "抖音",
        "base_url": "",
        "platform": "mock",
        "auth_type": "mock",
        "enabled": True,
    },
]


def _config_path() -> str:
    return os.environ.get("CHANNEL_CONFIG_FILE") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "channels.json")
    )


def _default_payload() -> dict[str, Any]:
    return {"channels": list(_DEFAULT_CHANNELS)}


class _Store:
    """JSON 配置读写 + mtime 缓存（避免每次调用读盘，跨进程用 mtime 检测变更）。"""

    def __init__(self) -> None:
        self._mtime: float = -1
        self._channels: Optional[list[dict[str, Any]]] = None
        self._dirty: bool = False

    def load(self) -> list[dict[str, Any]]:
        path = _config_path()
        try:
            mtime = os.path.getmtime(path)
            if mtime != self._mtime:
                with open(path, "r", encoding="utf-8") as f:
                    import json

                    data = json.load(f)
                self._channels = data.get("channels", list(_DEFAULT_CHANNELS))
                self._mtime = mtime
        except (OSError, ValueError):
            # 文件缺失/损坏：退回默认三渠道（内存）
            if self._channels is None:
                self._channels = list(_DEFAULT_CHANNELS)
        return list(self._channels) if self._channels is not None else list(_DEFAULT_CHANNELS)

    def save(self, channels: list[dict[str, Any]]) -> None:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            import json

            json.dump({"channels": channels}, f, ensure_ascii=False, indent=2)
        self._mtime = os.path.getmtime(path)
        self._channels = [dict(c) for c in channels]


_store = _Store()


class ChannelRegistry:
    """渠道配置注册表：增删改查 + 每渠道 REST client 工厂。"""

    def __init__(self) -> None:
        self._clients: dict[str, tuple[tuple, ChannelRestClient]] = {}

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        """返回渠道配置（api_key 等敏感字段已掩码）。"""
        return [self._masked(self._backfill_legacy(dict(c))) for c in _store.load()]

    def get(self, name: str) -> Optional[dict[str, Any]]:
        for c in _store.load():
            if c["name"] == name:
                return self._backfill_legacy(dict(c))
        return None

    @staticmethod
    def _backfill_legacy(cfg: dict[str, Any]) -> dict[str, Any]:
        """旧版 channels.json 缺 platform/options 字段时回填默认值（load 不做 normalize）。"""
        cfg.setdefault("platform", "mock")
        cfg.setdefault("options", {})
        return cfg

    def enabled_names(self) -> set[str]:
        """返回全部已启用渠道名（供 mock 平台放行）。"""
        return {c["name"] for c in _store.load() if c.get("enabled", True)}

    def add(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """新增渠道：name 唯一校验，返回规范化后的配置（含敏感字段掩码）。"""
        channels = _store.load()
        if any(c["name"] == cfg["name"] for c in channels):
            raise ValueError(f"渠道已存在: {cfg['name']}")
        normalized = self._normalize(cfg)
        channels.append(normalized)
        _store.save(channels)
        self._drop_client(cfg["name"])
        return self._masked(dict(normalized))

    def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        """更新渠道（改 base_url / auth / enabled 等），命中触发 client 重建。"""
        channels = _store.load()
        for c in channels:
            if c["name"] == name:
                merged = c | patch
                c.clear()
                c.update(self._normalize(merged))
                _store.save(channels)
                self._drop_client(name)
                return dict(c)
        raise KeyError(name)

    def remove(self, name: str) -> None:
        """删除渠道（内置三渠道不可删，用于保留演示基线）。"""
        if name in {"taobao", "jd", "douyin"}:
            raise ValueError("内置渠道（淘宝/京东/抖音）不可删除")
        channels = [c for c in _store.load() if c["name"] != name]
        if len(channels) == len(_store.load()):
            raise KeyError(name)
        _store.save(channels)
        self._drop_client(name)

    # ------------------------------------------------------------------
    # per-channel REST client
    # ------------------------------------------------------------------

    def client_for(self, channel: str) -> Optional[ChannelRestClient]:
        """按渠道配置返回对应 REST client；渠道不存在/未启用返回 None。

        每渠道按 (platform, base_url, auth_header) 缓存；配置变更后自动重建。
        离线 ASGI 兜底仅对 platform=mock 生效：真实平台（taobao/jd/douyin/open）
        一律走真实 TCP，且不注入 mock 默认鉴权头。
        """
        cfg = self.get(channel)
        if cfg is None or not cfg.get("enabled", True):
            return None

        platform = str(cfg.get("platform") or "mock")
        base_url = (cfg.get("base_url") or "").strip()
        headers = self._auth_headers(cfg)

        key = (platform, base_url, tuple(sorted(headers.items())))
        cached = self._clients.get(channel)
        if cached is not None and cached[0] == key:
            return cached[1]

        is_mock = not platform_is_real(platform)
        # 真实平台绝不携带 mock 默认鉴权头（default_headers={} 抑制）；
        # mock 平台沿用默认头即可（auth_header 已含 X-Api-Key，合并后一致）。
        default_headers = {} if not is_mock else None
        if not base_url:
            from os import environ

            url = environ.get("CHANNEL_API_URL") or DEFAULT_PLATFORM_URL
            transport = None
            # 只有 mock 平台才能走离线 ASGI 兜底；真实平台缺 base_url 时也试真实 TCP
            if is_mock and not environ.get("CHANNEL_API_URL"):
                from httpx import ASGITransport
                from mock_commerce.routes import app as _api_app

                transport = ASGITransport(_api_app)
            client = ChannelRestClient(
                base_url=url,
                auth_header=headers,
                transport=transport,
                default_headers=default_headers,
            )
        else:
            client = ChannelRestClient(
                base_url=base_url,
                auth_header=headers,
                default_headers=default_headers,
            )
        self._clients[channel] = (key, client)
        return client

    def executor_for(self, channel: Optional[str]):
        """按渠道 platform 字段返回对应的 PlatformAdapter 实例。

        channel 为 None（平台级操作，如知识库）时返回 mock 适配器。
        未知平台回退 mock，保证默认行为不因配置异常而退化。
        """
        if channel is None:
            return get_adapter("mock")
        cfg = self.get(channel)
        if cfg is None:
            return get_adapter("mock")
        return get_adapter(str(cfg.get("platform") or "mock"))

    def _auth_headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        """构造该渠道的鉴权头。

        mock 平台: X-Api-Key = api_key（若有）或 mock key。
        真实平台: 由 platform adapter 后续接管（base_url + options 签名），
        本层不注入 mock 专用头，避免向真实平台泄漏。
        """
        platform = str(cfg.get("platform") or "mock")
        if not platform_is_real(platform):
            return {"X-Api-Key": self._auth_key(cfg)}
        return {}

    def _auth_key(self, cfg: dict[str, Any]) -> str:
        """决定该渠道的鉴权 key：auth_type=api_key 显式配置优先，否则回退 mock key。"""
        if cfg.get("auth_type") == "api_key" and cfg.get("api_key"):
            return str(cfg["api_key"])
        return MOCK_API_KEY

    def _drop_client(self, name: str) -> None:
        old = self._clients.pop(name, None)
        if old is not None:
            try:
                import asyncio

                asyncio.create_task(old[1].aclose())
            except RuntimeError:
                pass

    @staticmethod
    def _masked(cfg: dict[str, Any]) -> dict[str, Any]:
        """掩码敏感字段（api_key + options 内的 app_secret/access_token/app_key）。

        返回新 dict（含重建的 options），绝不原地改写入参——避免掩码泄漏进
        注册表内存/落盘的共享 options 对象。
        """
        out = dict(cfg)
        if out.get("api_key"):
            out["api_key"] = _mask_secret(out["api_key"])
        opts = out.get("options")
        if isinstance(opts, dict):
            masked = {}
            for k, v in opts.items():
                if k in _SENSITIVE and isinstance(v, str) and v:
                    masked[k] = _mask_secret(v)
                else:
                    masked[k] = v
            out["options"] = masked
        return out

    @staticmethod
    def _normalize(cfg: dict[str, Any]) -> dict[str, Any]:
        """补齐字段并强制合法类型。"""
        platform = str(cfg.get("platform") or "mock").strip().lower()
        if platform not in PLATFORM_KINDS:
            platform = "mock"
        base: dict[str, Any] = {
            "name": str(cfg["name"]),
            "label": str(cfg.get("label") or cfg["name"]),
            "base_url": str(cfg.get("base_url") or "").strip(),
            "platform": platform,
            "auth_type": str(cfg.get("auth_type") or "mock"),
            "enabled": bool(cfg.get("enabled", True)),
            "created_at": cfg.get("created_at") or int(time.time()),
        }
        if cfg.get("api_key"):
            base["api_key"] = str(cfg["api_key"])
        options = cfg.get("options")
        if isinstance(options, dict) and options:
            base["options"] = {str(k): v for k, v in options.items()}
        return base


def _mask_secret(value: str) -> str:
    """前 4 位保留 + **** 掩码（短字段整体掩码）。"""
    return value[:4] + "****" if len(value) > 4 else "****"


# 全局默认注册表（主进程 / MCP Server / mock 平台共享同一配置源）
DEFAULT_CHANNEL_REGISTRY = ChannelRegistry()