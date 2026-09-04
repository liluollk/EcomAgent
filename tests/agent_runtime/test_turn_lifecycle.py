"""测试 TurnLifecycle — 技能解析、前置条件和系统提示词。"""

import pytest
from session.session import Session, PermissionMode
from session.workspace import Workspace
from agent_runtime.turn_lifecycle import TurnLifecycle


class FakeBackend:
    def capabilities(self):
        return None


def test_turn_lifecycle_resolve_skills():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    backend = FakeBackend()
    lifecycle = TurnLifecycle(session, ws, backend)

    skills = lifecycle.resolve_skills("查询淘宝库存")
    assert "inventory_query" in skills

    skills = lifecycle.resolve_skills("调整价格")
    assert "price_management" in skills

    skills = lifecycle.resolve_skills("今天天气怎么样")
    assert "general_query" in skills


def test_turn_lifecycle_check_prerequisites_with_sources():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    session.active_sources = ["taobao"]
    backend = FakeBackend()
    lifecycle = TurnLifecycle(session, ws, backend)
    assert lifecycle.check_prerequisites(["inventory_query"])


def test_turn_lifecycle_check_prerequisites_without_sources():
    ws = Workspace(workspace_id="ws-001", name="test")
    session = Session(session_id="sess-001", workspace=ws)
    backend = FakeBackend()
    lifecycle = TurnLifecycle(session, ws, backend)
    assert not lifecycle.check_prerequisites(["inventory_query"])


def test_turn_lifecycle_build_system_prompt():
    ws = Workspace(
        workspace_id="ws-001",
        name="OceanBreeze",
        metadata={"brand": "OceanBreeze"},
    )
    session = Session(session_id="sess-001", workspace=ws)
    session.active_sources = ["taobao", "jd"]
    backend = FakeBackend()
    lifecycle = TurnLifecycle(session, ws, backend)
    prompt = lifecycle.build_system_prompt()
    assert "OceanBreeze" in prompt
    assert "taobao" in prompt
    assert "jd" in prompt
    assert "ASK" in prompt