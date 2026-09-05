"""MCP Server 配置注册表 — data/mcp_servers.json（用户可前端增删改）。

模式对齐 channel_registry：JSON 持久化 + mtime 跨进程缓存 +
env 覆盖（MCP_SERVERS_CONFIG_FILE）+ 内置默认（外部工具演示服务）。

配置条目：
    {
      "name":    服务标识（唯一）,
      "command": 启动命令（如 python 可执行文件路径）,
      "args":    命令参数列表（如 ["-m", "mocks.mcp_tool_server"]）,
      "env":     附加环境变量（与进程环境合并后传给子进程）,
      "enabled": 是否启用
    }
"""

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
        os.path.join(os.path.dirname(__file__), "..", "data", "mcp_servers.json")
    )


def _default_servers() -> list[dict[str, Any]]:
    """内置默认：外部工具演示服务（mocks/mcp_tool_server.py）。"""
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
    """JSON 配置读写 + mtime 缓存（跨进程 mtime 检测变更）。"""

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
    """配置条目 → StdioServerParameters（env 与当前进程环境合并）。"""
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
    """MCP Server 配置注册表：CRUD + 启用过滤（前端可新增/启停/删除）。"""

    def list(self) -> list[dict[str, Any]]:
        """全部 server 配置（含 disabled）。"""
        return _store.load()

    def enabled_configs(self) -> list[dict[str, Any]]:
        """启用的 server 配置（连接时消费）。"""
        return [s for s in _store.load() if s.get("enabled", True)]

    def get(self, name: str) -> Optional[dict[str, Any]]:
        for s in _store.load():
            if s.get("name") == name:
                return s
        return None

    def add(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """新增 server：name/command 必填且唯一。"""
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
        """更新 server（command/args/env/enabled）。"""
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
        """删除 server 配置。"""
        servers = _store.load()
        kept = [s for s in servers if s.get("name") != name]
        if len(kept) == len(servers):
            raise KeyError(name)
        _store.save(kept)

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        return self.update(name, {"enabled": enabled})


# 模块级默认注册表（transport 路由与 mcp_pool 连接时消费）
DEFAULT_MCP_SERVER_REGISTRY = McpServerRegistry()