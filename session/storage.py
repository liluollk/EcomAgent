"""
JSONL 会话持久化 — 将 Session 消息与审计记录增量写入 JSONL 文件。

JSONL 持久化机制。
区分运行时状态（Session 对象）与持久化存储（JSONL 文件），
支持会话恢复和审计追踪。

文件格式（每行一条 JSON 记录）：
- 消息: {"role", "content", "timestamp"[, "tool_calls", "tool_call_id"]}
- 工具调用审计: {"record": "tool_call", "tool_name", "tool_use_id", "input", "result", ...}
- 权限请求审计: {"record": "permission_request", "request_id", "tool_name", "approved", ...}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .session import Session


@dataclass
class StoredMessage:
    """持久化存储的消息结构。

    Attributes:
        role: 消息角色。
        content: 消息内容。
        timestamp: 消息时间戳（ISO 8601 格式）。
        tool_calls: assistant 消息的 tool_calls 结构（纯文本消息为 None）。
        tool_call_id: role="tool" 消息对应的工具调用 ID（其他消息为 None）。
    """

    role: str
    content: str
    timestamp: str
    tool_calls: Optional[list[dict]] = None
    tool_call_id: Optional[str] = None


@dataclass
class StoredSession:
    """会话完整持久化记录。

    Attributes:
        user: 会话元数据（user_id / role）。
        model_state: 会话元数据（provider / model 快照，恢复时沿用）。
        messages: 消息列表（含工具调用消息结构）。
        tool_calls: 工具调用审计记录。
        permission_requests: 权限请求审计记录。
    """

    user: Optional[dict] = None
    model_state: Optional[dict] = None
    permission_mode: Optional[str] = None
    workspace_id: Optional[str] = None
    messages: list[StoredMessage] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    permission_requests: list[dict] = field(default_factory=list)


def save_session(session: Session, storage_dir: str) -> None:
    """将 Session 中未持久化的消息与审计记录增量写入 JSONL 文件。

    使用 mode="a" 追加写入，每条记录一行 JSON。
    首次写入时附带输出会话元数据（user）记录，供恢复时还原身份。
    写入后调用 session.mark_saved() 更新增量计数。

    Args:
        session: 要持久化的 Session 对象。
        storage_dir: 存储目录路径。
    """
    os.makedirs(storage_dir, exist_ok=True)
    filepath = os.path.join(storage_dir, f"{session.session_id}.jsonl")
    first_write = not os.path.exists(filepath)
    with open(filepath, "a", encoding="utf-8") as f:
        if first_write:
            f.write(json.dumps({
                "record": "session_meta",
                "session_id": session.session_id,
                "user": session.user,
                "model_state": session.model_state,
                "permission_mode": session.permission_mode.name,
                "workspace_id": session.workspace.workspace_id,
                "created_at": session.created_at.isoformat(),
            }, ensure_ascii=False) + "\n")
        for msg in session.get_unsaved_messages():
            payload = {
                "role": msg["role"],
                "content": msg["content"],
                "timestamp": msg["timestamp"],
            }
            # 结构化字段仅在有值时写入，保持普通消息行与旧版格式一致
            if msg.get("tool_calls"):
                payload["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                payload["tool_call_id"] = msg["tool_call_id"]
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        for record in session.get_unsaved_tool_calls():
            f.write(json.dumps({"record": "tool_call", **record}, ensure_ascii=False) + "\n")
        for record in session.get_unsaved_permission_requests():
            f.write(json.dumps({"record": "permission_request", **record}, ensure_ascii=False) + "\n")
    session.mark_saved()


def load_session_full(session_id: str, storage_dir: str) -> StoredSession:
    """从 JSONL 文件加载指定会话的全部记录（消息 + 审计）。

    兼容旧版格式：无 tool_calls / tool_call_id / record 字段的行按消息处理。

    Args:
        session_id: 会话唯一标识。
        storage_dir: 存储目录路径。

    Returns:
        StoredSession: 全部持久化记录；文件不存在时返回空结构。
    """
    filepath = os.path.join(storage_dir, f"{session_id}.jsonl")
    loaded = StoredSession()
    if not os.path.exists(filepath):
        return loaded
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            record = data.get("record", "message")
            if record == "tool_call":
                loaded.tool_calls.append(data)
            elif record == "permission_request":
                loaded.permission_requests.append(data)
            elif record == "session_meta":
                loaded.user = data.get("user")
                loaded.model_state = data.get("model_state")
                loaded.permission_mode = data.get("permission_mode")
                loaded.workspace_id = data.get("workspace_id")
            else:
                loaded.messages.append(StoredMessage(
                    role=data["role"],
                    content=data["content"],
                    timestamp=data["timestamp"],
                    tool_calls=data.get("tool_calls"),
                    tool_call_id=data.get("tool_call_id"),
                ))
    return loaded


def load_session(session_id: str, storage_dir: str) -> list[StoredMessage]:
    """从 JSONL 文件加载指定会话的所有消息。

    Args:
        session_id: 会话唯一标识。
        storage_dir: 存储目录路径。

    Returns:
        list[StoredMessage]: 所有消息的列表，按写入顺序排列。
    """
    return load_session_full(session_id, storage_dir).messages
