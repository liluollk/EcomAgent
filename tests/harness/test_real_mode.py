"""真实模型评测模式支撑：执行契约子集 + relaxed 断言语义。"""

from events.agent_event import CompleteEvent, ToolResultEvent, ToolStartEvent
from harness.assertions import NotExercisedError, assert_step
from harness.cases import SCENARIOS, real_model_scenarios


def _events(with_load_skill=True, tool="update_price", input_=None,
            result="平台 taobao 商品 ITEM-1001/SKU-002 价格已更新为 99.0 元（平台回查一致）"):
    evs = []
    if with_load_skill:
        evs.append(ToolStartEvent(tool_name="load_skill", tool_use_id="l1", input={"skill_name": "x"}))
        evs.append(ToolResultEvent(tool_use_id="l1", tool_name="load_skill", result="已加载"))
    evs.append(ToolStartEvent(tool_name=tool, tool_use_id="t1",
                              input=input_ or {"platform": "taobao", "product_id": "ITEM-1001",
                                               "sku_id": "SKU-002", "target_price": 99.0}))
    evs.append(ToolResultEvent(tool_use_id="t1", tool_name=tool, result=result))
    evs.append(CompleteEvent())
    return evs


_STEP = {"message": "把淘宝 ITEM-1001/SKU-002 的价格调到 99", "tool": "update_price",
         "input": {"platform": "taobao", "product_id": "ITEM-1001", "sku_id": "SKU-002",
                   "target_price": 99.0},
         "result_contains": ["价格已更新为"]}


def test_real_model_scenarios_is_subset_of_execution_tier():
    subset = real_model_scenarios()
    assert subset and all(s.get("real") for s in subset)
    assert len(subset) < len(SCENARIOS)
    names = {s["name"] for s in subset}
    assert "cost_protection_blocks_update" in names and "rate_limit_retry_same_key" in names
    assert "snapshot_query_two_platforms" not in names  # 纯决策契约场景不在真实模式


def test_relaxed_skips_load_skill_and_input_subset():
    """真实模型模式：无 load_skill、入参不精确匹配，仍判执行契约通过。"""
    evs = _events(with_load_skill=False,
                  input_={"platform": "taobao", "sku_id": "SKU-002", "target_price": 99.0})
    assert_step("x", _STEP, evs, relaxed=True)  # 不抛异常


def test_strict_mode_still_requires_load_skill():
    evs = _events(with_load_skill=False)
    try:
        assert_step("x", _STEP, evs, relaxed=False)
        assert False, "严格模式应要求 load_skill"
    except AssertionError:
        pass


def test_relaxed_expected_tool_missing_raises_not_exercised():
    """期望工具完全未出现 = not exercised（非执行失败），与断言失败区分。"""
    evs = _events(with_load_skill=False, tool="query_product_snapshot")
    try:
        assert_step("x", _STEP, evs, relaxed=True)
        assert False, "期望工具未出现应抛 NotExercisedError"
    except NotExercisedError:
        pass


def test_relaxed_allows_pre_read_before_expected_tool():
    """契约触发口径：期望工具出现在业务序列中即可——先读后写不判负。"""
    _STEP_ERR = {"message": "把淘宝 ITEM-1001/SKU-002 的价格调到 10", "tool": "update_price",
                 "input": {"platform": "taobao", "sku_id": "SKU-002", "target_price": 10.0},
                 "result_is_error": True, "result_contains": ["拦截", "成本"]}
    evs = [
        ToolStartEvent(tool_name="query_product_snapshot", tool_use_id="q1",
                       input={"platform": "taobao", "product_id": "ITEM-1001", "sku_id": "SKU-002"}),
        ToolResultEvent(tool_use_id="q1", tool_name="query_product_snapshot",
                        result="平台 taobao 商品 ITEM-1001/SKU-002：当前价 89.00 元"),
        ToolStartEvent(tool_name="update_price", tool_use_id="t1",
                       input={"platform": "taobao", "sku_id": "SKU-002", "target_price": 10.0}),
        # 早闸拦截：错误结果文本含「拦截」「成本」
        ToolResultEvent(tool_use_id="t1", tool_name="update_price",
                        result="拦截：价格 10.0 低于成本价 59.0，不允许调整",
                        is_error=True),
        CompleteEvent(),
    ]
    # relaxed：即使先查快照，结果断言归属 update_price 自身，契约通过
    assert_step("cost_protection_blocks_update", _STEP_ERR, evs, relaxed=True)


def test_strict_mode_requires_expected_tool_first():
    """剧本模式：第一个业务工具必须就是期望工具，先读后写仍判失败。"""
    evs = [
        ToolStartEvent(tool_name="load_skill", tool_use_id="l1", input={}),
        ToolResultEvent(tool_use_id="l1", tool_name="load_skill", result="已加载"),
        ToolStartEvent(tool_name="query_product_snapshot", tool_use_id="q1", input={}),
        ToolResultEvent(tool_use_id="q1", tool_name="query_product_snapshot", result="当前价 89.00 元"),
        ToolStartEvent(tool_name="update_price", tool_use_id="t1", input={}),
        ToolResultEvent(tool_use_id="t1", tool_name="update_price", result="已更新"),
        CompleteEvent(),
    ]
    try:
        assert_step("x", _STEP, evs, relaxed=False)
        assert False, "严格模式要求第一个业务工具是 update_price"
    except AssertionError:
        pass
