"""上下文压缩 + 长期记忆测试（索引文件式）。

覆盖：
- ContextCompressor：动态阈值、自动/手动压缩、结构化摘要字段、消息结构合法
- MemoryStore：自动提取 / 显式记住与忘记 / MEMORY.md 索引 / 四类型 / 检索上限
- BaseAgent 集成：超长会话触发压缩事件；turn 结束后记忆沉淀
"""

import asyncio
import os

import pytest

from agent_runtime.context_policy import ContextCompressor, context_threshold
from agent_runtime.memory_store import MemoryStore
from agent_runtime.base_agent import BaseAgent
from agent_backend.protocol import BackendConfig, BackendProvider
from agent_backend.factory import create_backend
from session.session import Session, PermissionMode
from session.workspace import Workspace


def _msg(role: str, content: str = "", tool_calls=None, tool_call_id: str = "") -> dict:
    m = {"role": role, "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    if tool_call_id:
        m["tool_call_id"] = tool_call_id
    return m


def _tool_call(tool_use_id: str, name: str, arguments: str) -> dict:
    return {"id": tool_use_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


# ---------------------------------------------------------------------------
# 上下文压缩
# ---------------------------------------------------------------------------

def test_threshold_from_window():
    assert context_threshold(128000) == 128000 - 13000
    assert context_threshold(32000) == 19000
    assert context_threshold(1000) >= 1000  # 下限保护


def test_no_collapse_when_short():
    comp = ContextCompressor(threshold_chars=20000)
    msgs = [_msg("user", "你好"), _msg("assistant", "好的")]
    new, changed = comp.collapse(msgs)
    assert changed is False and new is msgs


def test_auto_collapse_when_over_threshold():
    comp = ContextCompressor(threshold_chars=2000, keep_recent=3)
    early = [_msg("user", "库存问题 " * 500)] * 6          # 每条 2500 字符
    tail = _msg("user", "最新请求")
    msgs = early + [tail]
    new, changed = comp.collapse(msgs)
    assert changed is True
    assert new[0]["role"] == "system"
    assert "[会话摘要·结构化]" in new[0]["content"]
    assert new[-1] is tail                               # 近期原文保留
    assert sum(len(m.get("content") or "") for m in new) < 6000


def test_structured_summary_fields():
    comp = ContextCompressor(threshold_chars=5, keep_recent=1)
    msgs = [
        _msg("user", "把淘宝价格改为 100，成本 50"),
        _msg("assistant", "", tool_calls=[
            _tool_call("t1", "update_price", '{"channel": "taobao", "sku": "SKU-001"}'),
            _tool_call("t2", "query_inventory", '{"channel": "taobao"}')]),
        _msg("tool", "价格已更新为 100 元", tool_call_id="t1"),
        _msg("tool", "库存 1523", tool_call_id="t2"),
    ]
    new, changed = comp.collapse(msgs)
    assert changed is True
    content = new[0]["content"]
    assert "目标：" in content and "把淘宝价格改为 100" in content
    assert "已用工具：" in content and "update_price" in content
    assert "涉及渠道" in content and "taobao" in content
    assert "进度" in content and "价格已更新为 100 元" in content


def test_tool_message_not_leading_after_collapse():
    comp = ContextCompressor(threshold_chars=5, keep_recent=1)
    msgs = [
        _msg("user", "改价"),
        _msg("assistant", "", tool_calls=[_tool_call("t1", "update_price", "{}")]),
        _msg("tool", "结果1", tool_call_id="t1"),
    ]
    new, changed = comp.collapse(msgs)
    assert changed is True
    assert new[1]["role"] != "tool"


def test_manual_collapse_entries():
    comp = ContextCompressor(threshold_chars=1000, keep_recent=2)
    entries = [{"role": "user", "content": "x" * 400, "timestamp": f"t{i}"} for i in range(6)]
    new = comp.collapse_session_entries(entries)
    assert len(new) == 3                       # 1 摘要 + 2 近期
    assert new[0]["role"] == "system" and "timestamp" in new[0]


# ---------------------------------------------------------------------------
# 长期记忆（索引 + 独立文件）
# ---------------------------------------------------------------------------

@pytest.fixture
def memory(tmp_path):
    return MemoryStore(root=str(tmp_path / "memory"))


def _turn_msgs(tool="query_inventory", use="t1"):
    return [
        _msg("user", f"调用 {tool}"),
        _msg("assistant", "", tool_calls=[_tool_call(use, tool, '{"channel": "taobao"}')]),
        _msg("tool", "返回结果内容", tool_call_id=use),
    ]


def test_auto_extract_writes_index_and_file(memory, tmp_path):
    action, name = memory.handle_turn("default", "调用 query_inventory", _turn_msgs())
    assert action == "store" and name.startswith("经营记录_")
    ws_dir = tmp_path / "memory" / "default"
    files = os.listdir(ws_dir)
    assert "MEMORY.md" in files
    md = [f for f in files if f.endswith(".md") and f != "MEMORY.md"]
    assert len(md) == 1
    index = open(os.path.join(ws_dir, "MEMORY.md"), encoding="utf-8").read()
    assert "- [" in index and "(type: project)" in index


def test_auto_extract_skips_pure_chat(memory):
    action, name = memory.handle_turn("default", "随便聊聊", [
        _msg("user", "随便聊聊"), _msg("assistant", "好的"),
    ])
    assert action == "skip"


def test_auto_extract_feedback_type(memory):
    action, name = memory.handle_turn("default", "以后不要默认调库存", _turn_msgs())
    assert action == "store"
    ws_dir = memory._ws_dir("default")
    mdfile = [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"][0]
    content = open(os.path.join(ws_dir, mdfile), encoding="utf-8").read()
    assert "type: feedback" in content


def test_remember_explicit_project_and_user(memory):
    action, name = memory.handle_turn("default", "请记住：商品上线前必须确认库存")
    assert action == "store"
    ws_dir = memory._ws_dir("default")
    md = [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"]
    assert len(md) == 1
    content = open(os.path.join(ws_dir, md[0]), encoding="utf-8").read()
    assert "type: project" in content and "商品上线前必须确认库存" in content

    action, name = memory.handle_turn("default", "记住：我喜欢每天早上 9 点看报告")
    assert action == "store"
    md = [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"]
    content = next(open(os.path.join(ws_dir, f), encoding="utf-8").read() for f in md if "9 点" in open(os.path.join(ws_dir, f), encoding="utf-8").read())
    assert "type: user" in content


def test_remember_explicit_reference(memory):
    """记到参考 / 记住reference 写入 reference 类型（非 project/user）。"""
    action, name = memory.handle_turn("default", "记到参考：双11 平台补贴政策——满 200 减 30")
    assert action == "store"
    ws_dir = memory._ws_dir("default")
    md = [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"]
    content = next(open(os.path.join(ws_dir, f), encoding="utf-8").read() for f in md)
    assert "type: reference" in content
    assert "双11 平台补贴政策" in content

    # 别名语法同样生效
    action, name = memory.handle_turn("default", "记住reference：退货流程规范——拆包后 48 小时处理")
    assert action == "store"
    ws_dir = memory._ws_dir("default")
    all_md = [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"]
    ref_content = next(open(os.path.join(ws_dir, f), encoding="utf-8").read() for f in all_md if "退货流程" in open(os.path.join(ws_dir, f), encoding="utf-8").read())
    assert "type: reference" in ref_content


def test_forget_explicit(memory):
    memory.handle_turn("default", "请记住：临时知识 X")
    action, name = memory.handle_turn("default", "忘记：临时知识 X")
    assert action == "forgot" and "临时知识" in name
    ws_dir = memory._ws_dir("default")
    md = [f for f in os.listdir(ws_dir) if f.endswith(".md") and f != "MEMORY.md"]
    assert md == []                                    # 已删除
    index = open(os.path.join(ws_dir, "MEMORY.md"), encoding="utf-8").read()
    assert "临时知识 X" not in index


def test_recall_section_and_selected(memory):
    memory.handle_turn("default", "记住：双11 备货 3 万件")
    memory.handle_turn("default", "调用 query_inventory", _turn_msgs())
    section = memory.recall_section("default", query="双11")
    assert "[经营长期记忆]" in section
    assert "3 万件" in section                          # 关键词命中文件全文注入
    assert memory.recall_section("empty_ws") == ""


def test_selected_files_cap(memory):
    for i in range(8):
        memory.handle_turn("default", f"记住：第 {i} 条运营规则内容")
    picked = memory.selected_files("default", query="运营规则")
    assert len(picked) <= 5


# ---------------------------------------------------------------------------
# BaseAgent 集成
# ---------------------------------------------------------------------------

def _make_agent():
    backend = create_backend(BackendConfig(provider=BackendProvider.MOCK, model="mock", api_key="x"))
    ws = Workspace(workspace_id="default", name="OceanBreeze", metadata={"brand": "OceanBreeze"})
    return BaseAgent(backend, ws), ws


def _make_session(ws, sid="s1"):
    return Session(session_id=sid, workspace=ws, permission_mode=PermissionMode.EXECUTE,
                   active_sources=["taobao"])


def _run_chat(agent, session, text):
    async def _run():
        events = []
        async for ev in agent.chat(session, text, [{"name": "query_inventory", "description": "", "parameters": {}}]):
            events.append(ev)
        return events
    return asyncio.run(_run())


def test_chat_writes_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    from agent_runtime import memory_store as ms_mod
    monkeypatch.setattr("agent_runtime.base_agent.DEFAULT_MEMORY_STORE",
                        ms_mod.MemoryStore(root=str(tmp_path / "memory")))

    agent, ws = _make_agent()
    session = _make_session(ws)
    _run_chat(agent, session, "查一下库存")
    section = ms_mod.DEFAULT_MEMORY_STORE.recall_section("default")
    assert "query_inventory" in section
    assert os.path.exists(os.path.join(str(tmp_path / "memory"), "default", "MEMORY.md"))


def test_chat_triggers_compaction_event(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    from agent_runtime import memory_store as ms_mod
    monkeypatch.setattr("agent_runtime.base_agent.DEFAULT_MEMORY_STORE",
                        ms_mod.MemoryStore(root=str(tmp_path / "memory")))

    agent, ws = _make_agent()
    session = _make_session(ws)
    # 预塞超长历史触发自动压缩（9 条 × 5000 字符 > mock 窗口 32k-13k=19k）
    for _ in range(9):
        session.add_message("user", "库存历史 " * 1000)
    events = _run_chat(agent, session, "查一下库存")
    assert any(getattr(e, "message", "").startswith("上下文超限") for e in events)


def test_manual_compact_via_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    agent, ws = _make_agent()
    session = _make_session(ws)
    for i in range(10):
        session.add_message("user", f"长历史记录 {i} " + "x" * 300)
    # 手动压缩不依赖阈值：direct compact_session
    changed = agent.compact_session(session)
    assert changed is True
    assert session.messages[0]["role"] == "system"
    assert "[会话摘要·结构化]" in session.messages[0]["content"]