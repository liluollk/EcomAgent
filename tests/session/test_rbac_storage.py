"""RBAC 会话元数据持久化测试 — user（user_id / role）写入、恢复与旧格式兼容。"""

import os
import tempfile

from session.session import Session
from session.workspace import Workspace
from session.storage import save_session, load_session_full


def test_user_persisted_as_session_meta():
    """首次持久化应写入 session_meta 记录，恢复时还原 user。"""
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(
        session_id="sess-rbac",
        workspace=ws,
        user={"user_id": "cs_01", "role": "customer_service"},
    )
    session.add_message("user", "查询订单")

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        full = load_session_full("sess-rbac", tmpdir)
        assert full.user == {"user_id": "cs_01", "role": "customer_service"}
        assert len(full.messages) == 1

        # 二次保存不重复写 meta
        session.add_message("assistant", "订单状态：已发货")
        save_session(session, tmpdir)
        full = load_session_full("sess-rbac", tmpdir)
        assert full.user == {"user_id": "cs_01", "role": "customer_service"}
        assert len(full.messages) == 2


def test_default_user_persisted():
    """未显式设置 user 时，默认运营角色也应持久化。"""
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-default", workspace=ws)

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        full = load_session_full("sess-default", tmpdir)
        assert full.user["role"] == "operator"


def test_model_state_persisted():
    """model_state（provider/model 快照）随会话元数据持久化并恢复。"""
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(
        session_id="sess-model",
        workspace=ws,
        user={"user_id": "u1", "role": "operator"},
        model_state={"provider": "anthropic", "model": "claude-3-5-sonnet-latest"},
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        save_session(session, tmpdir)
        full = load_session_full("sess-model", tmpdir)
        assert full.model_state == {"provider": "anthropic", "model": "claude-3-5-sonnet-latest"}
        # 旧格式文件（无 model_state）兼容
        legacy = os.path.join(tmpdir, "legacy2.jsonl")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"role": "user", "content": "hi", "timestamp": "t1"}\n')
        full2 = load_session_full("legacy2", tmpdir)
        assert full2.model_state is None


def test_legacy_file_without_meta_loads():
    """旧格式文件（无 session_meta 行）应正常加载，user 为 None。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "legacy.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"role": "user", "content": "hi", "timestamp": "t1"}\n')
        full = load_session_full("legacy", tmpdir)
        assert full.user is None
        assert len(full.messages) == 1