"""ProviderRegistry — 模型供应商配置化注册表（前端可配置供应商/模型/密钥的统一入口）。

目标：让「在设置页切换 LLM 供应商、切模型、填 API Key / base_url」成为可操作、可持久化、
即时起效的功能，替代运行前写死的环境变量（AGENT_BACKEND / AGENT_MODEL / OPENAI_API_KEY 等）。

设计（照抄 ChannelRegistry 的既成模式）：
- 供应商配置持久化到 JSON 文件（默认 data/providers.json，可用 PROVIDER_CONFIG_FILE 覆盖）。
  主进程管理 API 写入；MCP Server 子进程等共享读取同一文件（mtime 缓存检测变更）。
- 全局「当前激活供应商」决定新构建的 AgentBackend 用哪家；保存即生效，无需重启。
- 字段：name / label / provider / default_model / api_key / api_base / model_list / enabled。
  敏感字段 api_key 返回前掩码。
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from .protocol import BackendConfig, BackendProvider
from .factory import create_backend

# 键仅 mask 展示，不落明文返回给前端
_SENSITIVE = {"api_key"}

# 内置默认供应商（api_key 留空：填了真实密钥即接入真实 LLM，否则回退 demo-key）
_DEFAULT_PROVIDERS: list[dict[str, Any]] = [
    {
        "name": "openai",
        "label": "OpenAI",
        "provider": "openai",
        "default_model": "gpt-4o-mini",
        "api_key": "",
        "api_base": "",
        "model_list": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "enabled": True,
    },
    {
        "name": "anthropic",
        "label": "Anthropic",
        "provider": "anthropic",
        "default_model": "claude-3-5-sonnet-latest",
        "api_key": "",
        "api_base": "",
        "model_list": ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"],
        "enabled": True,
    },
    {
        "name": "mock",
        "label": "Mock（离线演示）",
        "provider": "mock",
        "default_model": "gpt-4o-mini",
        "api_key": "",
        "api_base": "",
        "model_list": [],
        "enabled": True,
    },
]

# 内置供应商名（不可删除，保留演示基线）
_BUILTIN = {"openai", "anthropic", "mock"}

# 各 provider 对应的环境变量回退（注册表未配置时回退到 env，再到内置默认）
_ENV_KEY: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_ENV_BASE: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
}
_ENV_MODEL: dict[str, str] = {
    "openai": "AGENT_MODEL",
    "anthropic": "AGENT_MODEL",
}


def _config_path() -> str:
    return os.environ.get("PROVIDER_CONFIG_FILE") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "providers.json")
    )


def _default_payload() -> dict[str, Any]:
    return {"providers": list(_DEFAULT_PROVIDERS), "active": "openai"}


class _Store:
    """JSON 配置读写 + mtime 缓存（避免每次调用读盘，跨进程用 mtime 检测变更）。"""

    def __init__(self) -> None:
        self._mtime: float = -1
        self._providers: Optional[list[dict[str, Any]]] = None
        self._active: str = "openai"
        self._dirty: bool = False

    def load(self) -> tuple[list[dict[str, Any]], str]:
        """返回 (providers, active_name)。文件缺失/损坏时回退内置默认。"""
        path = _config_path()
        try:
            mtime = os.path.getmtime(path)
            if mtime != self._mtime:
                with open(path, "r", encoding="utf-8") as f:
                    import json

                    data = json.load(f)
                self._providers = data.get("providers", list(_DEFAULT_PROVIDERS))
                self._active = data.get("active") or "openai"
                self._mtime = mtime
        except (OSError, ValueError, KeyError, TypeError):
            if self._providers is None:
                self._providers = list(_DEFAULT_PROVIDERS)
                self._active = "openai"
        return (
            [dict(p) for p in (self._providers or _DEFAULT_PROVIDERS)],
            self._active,
        )

    def save(self, providers: list[dict[str, Any]], active: str) -> None:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            import json

            json.dump(
                {"providers": providers, "active": active},
                f,
                ensure_ascii=False,
                indent=2,
            )
        self._mtime = os.path.getmtime(path)
        self._providers = [dict(c) for c in providers]
        self._active = active


_store = _Store()


class ProviderRegistry:
    """模型供应商注册表：增删改查 + 当前激活者 + 按配置创建 Backend 的工厂。"""

    def list(self) -> list[dict[str, Any]]:
        """返回全部供应商配置（api_key 等敏感字段已掩码）。"""
        providers, _ = _store.load()
        return [self._masked(dict(p)) for p in providers]

    def get(self, name: str) -> Optional[dict[str, Any]]:
        for p in _store.load()[0]:
            if p["name"] == name:
                return dict(p)
        return None

    def active_name(self) -> str:
        """当前激活供应商名（不存在时回退内置第一个）。"""
        providers, active = _store.load()
        names = [p["name"] for p in providers]
        return active if active in names else (names[0] if names else "openai")

    def get_active(self) -> Optional[dict[str, Any]]:
        """返回当前激活供应商的完整配置（含真实 api_key，供构建 Backend）。"""
        return self.get(self.active_name())

    def set_active(self, name: str) -> dict[str, Any]:
        """设置当前激活供应商。"""
        providers, _ = _store.load()
        if not any(p["name"] == name for p in providers):
            raise KeyError(name)
        _store.save(providers, name)
        cfg = self.get(name)
        assert cfg is not None
        return self._masked(dict(cfg))

    def add(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """新增供应商：name 唯一，provider 必须合法且非内置重复。"""
        providers, active = _store.load()
        name = str(cfg.get("name") or "").strip().lower()
        if not name:
            raise ValueError("name 必填")
        if any(p["name"] == name for p in providers):
            raise ValueError(f"供应商已存在: {name}")
        if name in _BUILTIN:
            raise ValueError(f"内置供应商 {name} 不可重复新增")
        provider = str(cfg.get("provider") or "").strip().lower()
        if provider not in {"openai", "anthropic", "mock"}:
            raise ValueError(f"不支持的 provider: {provider}")
        normalized = self._normalize({**cfg, "name": name, "provider": provider})
        providers.append(normalized)
        _store.save(providers, active)
        return self._masked(dict(normalized))

    def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        """更新供应商（改 api_key / api_base / default_model / enabled 等）。"""
        providers, active = _store.load()
        for p in providers:
            if p["name"] == name:
                merged = p | patch
                p.clear()
                p.update(self._normalize(merged))
                _store.save(providers, active)
                return dict(p)
        raise KeyError(name)

    def remove(self, name: str) -> None:
        """删除供应商（内置三供应商不可删）。激活中被删时切回内置第一个。"""
        if name in _BUILTIN:
            raise ValueError("内置供应商（openai/anthropic/mock）不可删除")
        providers, active = _store.load()
        if not any(p["name"] == name for p in providers):
            raise KeyError(name)
        providers = [p for p in providers if p["name"] != name]
        new_active = name if active != name else (providers[0]["name"] if providers else "openai")
        _store.save(providers, new_active)

    # ------------------------------------------------------------------
    # 值回退 & Backend 工厂
    # ------------------------------------------------------------------

    def _effective(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """按「配置 → 环境变量 → 内置默认」回退 api_key / api_base / model。"""
        provider = cfg.get("provider", "openai")
        model = (
            cfg.get("default_model")
            or os.environ.get(_ENV_MODEL.get(provider, ""))
            or ("claude-3-5-sonnet-latest" if provider == "anthropic" else "gpt-4o-mini")
        )
        api_key = cfg.get("api_key") or os.environ.get(_ENV_KEY.get(provider, "")) or "demo-key"
        api_base = cfg.get("api_base") or os.environ.get(_ENV_BASE.get(provider, "")) or None
        return {"provider": provider, "model": model, "api_key": api_key, "api_base": api_base or None}

    def _has_explicit_config(self) -> bool:
        """是否已有持久化的供应商配置（设置页保存过 providers.json 即视为显式配置）。"""
        return os.path.exists(_config_path())

    def effective_provider_name(self) -> str:
        """当前生效的供应商名（构建后端用）。

        优先级：显式持久化的激活供应商 > AGENT_BACKEND 环境变量（尚无配置时）> 'openai'。
        这样 `AGENT_BACKEND=mock` 的离线演示工作流（README）在未保存过设置时依然生效，
        而一旦在设置页激活了某个供应商并持久化，设置即优先于环境变量。
        """
        if not self._has_explicit_config():
            env_backend = os.environ.get("AGENT_BACKEND", "").strip().lower()
            if env_backend and self.get(env_backend) is not None:
                return env_backend
        return self.active_name()

    def build_config(self, name: Optional[str] = None) -> BackendConfig:
        """按指定或当前生效供应商构建 BackendConfig。缺失时回退默认 openai。"""
        pick = name or self.effective_provider_name()
        cfg = self.get(pick)
        if cfg is None:
            cfg = next((p for p in _DEFAULT_PROVIDERS if p["name"] == "openai"), _DEFAULT_PROVIDERS[0])
        eff = self._effective(cfg)
        provider = {
            "openai": BackendProvider.OPENAI,
            "anthropic": BackendProvider.ANTHROPIC,
            "mock": BackendProvider.MOCK,
        }.get(eff["provider"], BackendProvider.OPENAI)
        thinking_level = str(cfg.get("thinking_level") or "").strip().lower()
        return BackendConfig(
            provider=provider,
            model=eff["model"],
            api_key="" if provider == BackendProvider.MOCK else eff["api_key"],
            api_base=eff["api_base"],
            thinking_level=thinking_level if thinking_level in ("low", "medium", "high") else None,
        )

    @staticmethod
    def _masked(cfg: dict[str, Any]) -> dict[str, Any]:
        if cfg.get("api_key"):
            cfg["api_key"] = cfg["api_key"][:4] + "****"
        return cfg

    @staticmethod
    def _normalize(cfg: dict[str, Any]) -> dict[str, Any]:
        """补齐字段并强制合法类型。"""
        base: dict[str, Any] = {
            "name": str(cfg["name"]).strip().lower(),
            "label": str(cfg.get("label") or cfg["name"]),
            "provider": str(cfg.get("provider") or "openai").strip().lower(),
            "default_model": str(cfg.get("default_model") or "").strip(),
            "api_base": str(cfg.get("api_base") or "").strip(),
            "enabled": bool(cfg.get("enabled", True)),
            "created_at": cfg.get("created_at") or int(time.time()),
        }
        if cfg.get("api_key"):
            base["api_key"] = str(cfg["api_key"])
        # thinking_level 统一为 low/medium/high；非法值归 None（关闭）
        thinking_level = str(cfg.get("thinking_level") or "").strip().lower()
        base["thinking_level"] = thinking_level if thinking_level in ("low", "medium", "high") else None
        model_list = cfg.get("model_list")
        base["model_list"] = (
            [str(m) for m in model_list] if isinstance(model_list, list) else []
        )
        return base


# 全局默认注册表（主进程 / MCP Server 子进程共享同一配置源）
DEFAULT_PROVIDER_REGISTRY = ProviderRegistry()