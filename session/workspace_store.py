"""Workspace 配置的 JSON 持久化与规则规范化。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

KNOWN_RULE_TYPES = frozenset({"price_above_cost"})
DEFAULT_RULES = [{"type": "price_above_cost", "enabled": True}]


def _config_path() -> Path:
    configured = os.environ.get("WORKSPACE_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "data" / "workspaces.json"


def normalize_rules(value: Any, *, default: bool = True) -> list[dict[str, Any]]:
    """将前端规则配置规范化为服务端统一表示。"""
    if value is None:
        return [dict(rule) for rule in DEFAULT_RULES] if default else []
    if not isinstance(value, list):
        raise ValueError("rules 必须是数组")

    normalized: list[dict[str, Any]] = []
    for rule in value:
        if not isinstance(rule, dict):
            raise ValueError("规则必须是对象")
        rule_type = str(rule.get("type") or "").strip()
        if rule_type not in KNOWN_RULE_TYPES:
            raise ValueError(f"未知规则类型: {rule_type or '<empty>'}")
        enabled = rule.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("规则 enabled 必须是布尔值")
        if enabled:
            normalized.append({"type": rule_type, "enabled": True})
    return normalized


def load_workspace_records() -> list[dict[str, Any]]:
    path = _config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    records = data.get("workspaces", []) if isinstance(data, dict) else []
    return [dict(record) for record in records if isinstance(record, dict) and record.get("workspace_id")]


def load_workspace_record(workspace_id: str) -> dict[str, Any] | None:
    return next(
        (record for record in load_workspace_records() if record.get("workspace_id") == workspace_id),
        None,
    )


def save_workspace_records(records: list[dict[str, Any]]) -> None:
    """原子保存配置，避免进程中断留下半截 JSON。"""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"workspaces": records}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_workspace_record(record: dict[str, Any]) -> None:
    records = load_workspace_records()
    workspace_id = record["workspace_id"]
    replaced = False
    for index, current in enumerate(records):
        if current.get("workspace_id") == workspace_id:
            records[index] = dict(record)
            replaced = True
            break
    if not replaced:
        records.append(dict(record))
    save_workspace_records(records)
