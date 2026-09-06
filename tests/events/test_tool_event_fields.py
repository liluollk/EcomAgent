"""工具事件可观测性字段 — 缺省值不破坏既有消费方。"""

from events.agent_event import ToolResultEvent, ToolStartEvent


def test_tool_start_observability_defaults():
    ts = ToolStartEvent(tool_name="update_price", tool_use_id="t1", input={"sku": "SKU-1"})
    assert ts.type == "tool_start"
    assert ts.trace_id == ""
    assert ts.source == ""


def test_tool_result_observability_defaults():
    tr = ToolResultEvent(tool_use_id="t1", tool_name="update_price", result="ok")
    assert tr.attempt == 1
    assert tr.duration_ms == 0
    assert tr.idempotent_replay is False
    assert tr.trace_id == ""
    assert tr.source == ""
    assert tr.is_error is False
