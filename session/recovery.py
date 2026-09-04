"""
会话恢复 — 从 JSONL 持久化文件中恢复 Session 的消息历史。

的会话恢复功能。
当 Session 因异常中断后，可通过 JSONL 文件恢复消息历史，
继续执行未完成的 turn。
"""

from __future__ import annotations

from .session import Session
from .storage import load_session


def recover_session_messages(session: Session, storage_dir: str) -> Session:
    """从 JSONL 存储文件恢复 Session 的消息历史。

    加载持久化消息后，将消息填充到 Session 对象中，
    并调用 mark_saved() 标记这些消息已持久化。

    Args:
        session: 要恢复的 Session 对象（需已设置 session_id 和 workspace）。
        storage_dir: 存储目录路径。

    Returns:
        Session: 恢复后的 Session 对象（原地修改并返回）。
    """
    stored = load_session(session.session_id, storage_dir)
    for msg in stored:
        session.add_message(msg.role, msg.content)
    # 标记已加载的消息为已持久化，避免重复写入
    session.mark_saved()
    return session