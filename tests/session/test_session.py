"""测试 Session 创建、消息追加、恢复和状态管理。"""

import pytest
from session.session import Session, SessionStatus, PermissionMode
from session.workspace import Workspace


def test_session_creation():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    assert session.session_id == "sess-001"
    assert session.workspace == ws
    assert session.permission_mode == PermissionMode.ASK
    assert session.status == SessionStatus.ACTIVE


def test_session_add_message():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    session.add_message("user", "查询库存")
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "user"
    assert session.messages[0]["content"] == "查询库存"


def test_session_unsaved_messages():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    session.add_message("user", "消息1")
    assert len(session.get_unsaved_messages()) == 1
    session.mark_saved()
    assert len(session.get_unsaved_messages()) == 0
    session.add_message("assistant", "消息2")
    assert len(session.get_unsaved_messages()) == 1


def test_session_abort():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    session.abort()
    assert session.status == SessionStatus.ABORTED


def test_recover_session_messages():
    import tempfile
    from session.recovery import recover_session_messages
    from session.storage import save_session
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-003", workspace=ws)
    session.add_message("user", "历史消息1")
    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        new_session = Session(session_id="sess-003", workspace=ws)
        recovered = recover_session_messages(new_session, tmpdir)
        assert len(recovered.messages) == 1
        assert recovered.messages[0]["content"] == "历史消息1"