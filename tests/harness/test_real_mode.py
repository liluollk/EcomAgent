"""真实模型评测模式支撑：运行时契约子集 + relaxed 断言语义。"""

from events.agent_event import CompleteEvent, ToolResultEvent, ToolStartEvent
from harness.assertions import assert_step
from harness.cases import SCENARIOS, real_model_scenarios


def _events(with_load_skill=True, tool="update_price", input_=None, result="渠道 taobao 商品 SKU-001 价格已更新为 89.0 元"):
    evs = []
    if with_load_skill:
        evs.append(ToolStartEvent(tool_name="load_skill", tool_use_id="l1", input={"skill_name": "x"}))
        evs.append(ToolResultEvent(tool_use_id="l1", tool_name="load_skill", result="已加载"))
    evs.append(ToolStartEvent(tool_name=tool, tool_use_id="t1", input=input_ or {"channel": "taobao", "sku": "SKU-001"}))
    evs.append(ToolResultEvent(tool_use_id="t1", tool_name=tool, result=result))
    evs.append(CompleteEvent())
    return evs


_STEP = {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
         "input": {"channel": "taobao", "sku": "SKU-001"}, "result_contains": ["已更新"]}


def test_real_model_scenarios_is_subset_of_runtime_tier():
    subset = real_model_scenarios()
    assert subset and all(s.get("real") for s in subset)
    assert len(subset) < len(SCENARIOS)
    names = {s["name"] for s in subset}
    assert "cost_interception" in names and "upstream_429_retry_success" in names
    assert "six_step_business_chain" not in names  # 决策契约场景不在真实模式


def test_relaxed_skips_load_skill_and_input_subset():
    """真实模型模式：无 load_skill、入参不精确匹配，仍判运行时契约通过。"""
    evs = _events(with_load_skill=False, input_={"channel": "taobao", "sku": "SKU-001", "new_price": 89.0})
    assert_step("x", _STEP, evs, relaxed=True)  # 不抛异常


def test_strict_mode_still_requires_load_skill():
    evs = _events(with_load_skill=False)
    try:
        assert_step("x", _STEP, evs, relaxed=False)
        assert False, "严格模式应要求 load_skill"
    except AssertionError:
        pass


def test_relaxed_still_checks_tool_name():
    evs = _events(with_load_skill=False, tool="query_inventory")
    try:
        assert_step("x", _STEP, evs, relaxed=True)
        assert False, "工具名不符仍应失败"
    except AssertionError:
        pass
