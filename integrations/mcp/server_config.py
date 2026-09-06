"""MCP Server 配置注册表 — data/mcp_servers.json（用户可前端增删改）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from mcp import StdioServerParameters


def _config_path() -> str:
    env = os.environ.get("MCP_SERVERS_CONFIG_FILE")
    if env:
        return os.path.abspath(env)
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "mcp_servers.json")
    )


def _default_servers() -> list[dict[str, Any]]:
    return [
        {
            "name": "external-demo",
            "command": sys.executable,
            "args": ["-m", "mocks.mcp_tool_server"],
            "env": {},
            "enabled": True,
        }
    ]


class _Store:
    def __init__(self) -> None:
        self._mtime: float = -1
        self._servers: Optional[list[dict[str, Any]]] = None

    def load(self) -> list[dict[str, Any]]:
        path = _config_path()
        try:
            mtime = os.path.getmtime(path)
            if mtime != self._mtime:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._servers = data.get("servers", [])
                self._mtime = mtime
        except (OSError, ValueError):
            if self._servers is None:
                self._servers = [dict(s) for s in _default_servers()]
        return [dict(s) for s in self._servers] if self._servers is not None else [dict(s) for s in _default_servers()]

    def save(self, servers: list[dict[str, Any]]) -> None:
        path = _config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"servers": servers}, f, ensure_ascii=False, indent=2)
        self._mtime = os.path.getmtime(path)
        self._servers = [dict(s) for s in servers]


_store = _Store()


def to_params(cfg: dict[str, Any]) -> StdioServerParameters:
    env = dict(os.environ)
    extra = cfg.get("env")
    if isinstance(extra, dict):
        env.update({str(k): str(v) for k, v in extra.items()})
    return StdioServerParameters(
        command=str(cfg.get("command") or sys.executable),
        args=[str(a) for a in (cfg.get("args") or [])],
        env=env,
    )


class McpServerRegistry:
    def list(self) -> list[dict[str, Any]]:
        return _store.load()

    def enabled_configs(self) -> list[dict[str, Any]]:
        return [s for s in _store.load() if s.get("enabled", True)]

    def get(self, name: str) -> Optional[dict[str, Any]]:
        for s in _store.load():
            if s.get("name") == name:
                return s
        return None

    def add(self, cfg: dict[str, Any]) -> dict[str, Any]:
        name = str(cfg.get("name") or "").strip()
        command = str(cfg.get("command") or "").strip()
        if not name or not command:
            raise ValueError("name 与 command 必填")
        servers = _store.load()
        if any(s.get("name") == name for s in servers):
            raise ValueError(f"MCP server 已存在: {name}")
        entry = {
            "name": name,
            "command": command,
            "args": [str(a) for a in (cfg.get("args") or [])],
            "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
            "enabled": bool(cfg.get("enabled", True)),
        }
        servers.append(entry)
        _store.save(servers)
        return dict(entry)

    def update(self, name: str, patch: dict[str, Any]) -> dict[str, Any]:
        servers = _store.load()
        for i, s in enumerate(servers):
            if s.get("name") != name:
                continue
            merged = dict(s)
            for k in ("command", "args", "env", "enabled"):
                if k in patch:
                    merged[k] = patch[k]
            if not str(merged.get("command") or "").strip():
                raise ValueError("command 不能为空")
            servers[i] = merged
            _store.save(servers)
            return dict(merged)
        raise KeyError(name)

    def remove(self, name: str) -> None:
        servers = _store.load()
        kept = [s for s in servers if s.get("name") != name]
        if len(kept) == len(servers):
            raise KeyError(name)
        _store.save(kept)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        return self.update(name, {"enabled": enabled})


DEFAULT_MCP_SERVER_REGISTRY = McpServerRegistry()