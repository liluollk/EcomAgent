"""测试 JSONL 会话持久化 — 写入、读取和追加写入。"""

import json
import os
import tempfile
import pytest
from session.session import Session
from session.workspace import Workspace
from session.storage import save_session, load_session, load_session_full


def test_save_and_load_session():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    session.add_message("user", "查询库存")
    session.add_message("assistant", "库存查询结果：32件")

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        messages = load_session("sess-001", tmpdir)
        assert len(messages) == 2
        assert messages[0].content == "查询库存"
        assert messages[1].content == "库存查询结果：32件"


def test_append_session():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-002", workspace=ws)
    session.add_message("user", "第一条消息")

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        session.add_message("assistant", "第二条消息")
        save_session(session, tmpdir)

        messages = load_session("sess-002", tmpdir)
        assert len(messages) == 2
        assert messages[0].content == "第一条消息"
        assert messages[1].content == "第二条消息"


def test_load_nonexistent_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        messages = load_session("nonexistent", tmpdir)
        assert len(messages) == 0


def test_save_and_load_tool_records():
    """工具调用消息结构、审计记录与权限请求应完整持久化并可恢复。"""
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-tool", workspace=ws)
    session.add_message("user", "查询库存")
    session.add_message_record({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "c1",
            "type": "function",
            "function": {"name": "query_inventory", "arguments": "{\"channel\": \"taobao\"}"},
        }],
        "timestamp": "t1",
    })
    session.add_message_record({
        "role": "tool",
        "tool_call_id": "c1",
        "content": "库存 100",
        "timestamp": "t2",
    })
    session.add_tool_call({
        "tool_name": "query_inventory",
        "tool_use_id": "c1",
        "input": {"channel": "taobao"},
        "result": "库存 100",
        "is_error": False,
        "timestamp": "t2",
    })
    session.permission_requests.append({
        "request_id": "perm_c1",
        "tool_name": "update_price",
        "input": {},
        "approved": True,
        "timestamp": "t3",
    })

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        full = load_session_full("sess-tool", tmpdir)

        assert len(full.messages) == 3
        assert full.messages[0].tool_calls is None  # 普通消息不携带结构
        assert full.messages[1].tool_calls[0]["id"] == "c1"
        assert full.messages[2].tool_call_id == "c1"
        assert full.tool_calls[0]["tool_name"] == "query_inventory"
        assert full.tool_calls[0]["input"] == {"channel": "taobao"}
        assert full.permission_requests[0]["approved"] is True


def test_incremental_persist_marks_audit_records():
    """mark_saved 应同时推进消息与审计记录的增量计数，避免重复写入。"""
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-incr", workspace=ws)
    session.add_message("user", "第一条")
    session.add_tool_call({"tool_name": "t", "tool_use_id": "c", "result": "r"})
    session.permission_requests.append({"request_id": "p", "approved": True})

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        session.add_message("assistant", "第二条")
        save_session(session, tmpdir)

        full = load_session_full("sess-incr", tmpdir)
        assert len(full.messages) == 2
        assert len(full.tool_calls) == 1
        assert len(full.permission_requests) == 1


def test_backward_compat_plain_lines():
    """旧版格式（无 tool_calls / record 字段）可正常加载。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "legacy.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": "旧消息", "timestamp": "t"}, ensure_ascii=False) + "\n")

        full = load_session_full("legacy", tmpdir)
        assert len(full.messages) == 1
        assert full.messages[0].role == "user"
        assert full.messages[0].tool_calls is None
        assert full.tool_calls == []
        assert full.permission_requests == []


def test_persist_and_restore_permission_mode():
    from session.session import PermissionMode

    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-mode", workspace=ws, permission_mode=PermissionMode.EXECUTE)
    session.add_message("user", "hi")

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        full = load_session_full("sess-mode", tmpdir)
        assert full.permission_mode == "EXECUTE"


def test_restore_legacy_session_meta_missing_mode_is_none():
    """旧版 session_meta 无 permission_mode 字段时回退 None（由恢复侧落回 ASK），不报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "legacy-meta.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "record": "session_meta",
                "session_id": "legacy-meta",
                "user": {"user_id": "u", "role": "operator"},
                "model_state": {"provider": "openai", "model": "gpt-4o-mini"},
                "created_at": "t",
            }, ensure_ascii=False) + "\n")
        full = load_session_full("legacy-meta", tmpdir)
        assert full.permission_mode is None