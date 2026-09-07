"""Workspace 业务规则测试 — rules 声明接入 PreToolUse 管线的分派与短路。"""

from permission.rule_engine import workspace_rules_rule
from permission.pre_tool_use import PreToolUseAction


def test_price_above_cost_rule_dispatched():
    """默认规则 {type: price_above_cost} 拦截低于成本价的改价。"""
    gate = workspace_rules_rule([{"type": "price_above_cost"}])
    result = gate("update_price", {"new_price": 30, "cost_price": 50})
    assert result.action == PreToolUseAction.BLOCK
    assert "低于成本价" in result.reason
    result = gate("update_price", {"new_price": 80, "cost_price": 50})
    assert result.action == PreToolUseAction.ALLOW


def test_non_target_tool_not_affected():
    """规则只作用于其目标工具，其他工具放行。"""
    gate = workspace_rules_rule([{"type": "price_above_cost"}])
    assert gate("create_promotion", {}).action == PreToolUseAction.ALLOW
    assert gate("query_inventory", {}).action == PreToolUseAction.ALLOW


def test_unknown_rule_type_ignored():
    """未知规则类型跳过，不拦截。"""
    gate = workspace_rules_rule([{"type": "no_such_rule"}])
    assert gate("update_price", {"new_price": 1}).action == PreToolUseAction.ALLOW


def test_empty_rules_all_allow():
    """空规则列表全放行。"""
    gate = workspace_rules_rule([])
    assert gate("update_price", {"new_price": 1}).action == PreToolUseAction.ALLOW
    assert workspace_rules_rule(None)("update_price", {}).action == PreToolUseAction.ALLOW


def test_first_block_short_circuits():
    """多规则时首个非 ALLOW 结果短路返回。"""
    gate = workspace_rules_rule([{"type": "price_above_cost"}, {"type": "price_above_cost"}])
    result = gate("update_price", {"new_price": 10, "cost_price": 20})
    assert result.action == PreToolUseAction.BLOCK


def test_cost_lookup_overrides_model_input():
    """平台真相覆盖模型传入的 cost_price：模型谎报成本也越不过规则。"""
    # 模型谎报 cost_price=1（想让 20 通过），但平台真相是 59 → 仍拦截
    gate = workspace_rules_rule(
        [{"type": "price_above_cost"}],
        cost_lookup=lambda ch, sku: 59.0,
    )
    result = gate("update_price", {"channel": "taobao", "sku": "SKU-001", "new_price": 20, "cost_price": 1})
    assert result.action == PreToolUseAction.BLOCK
    assert "59.0" in result.reason  # 用的是平台真相，不是模型谎报的 1


def test_cost_lookup_allows_above_platform_cost():
    gate = workspace_rules_rule(
        [{"type": "price_above_cost"}],
        cost_lookup=lambda ch, sku: 59.0,
    )
    assert gate("update_price", {"channel": "taobao", "sku": "SKU-001", "new_price": 89}).action == PreToolUseAction.ALLOW


def test_cost_lookup_none_falls_back_to_model_input():
    """lookup 返回 None（未知 SKU/渠道）→ 回退 tool_input 自带 cost_price。"""
    gate = workspace_rules_rule(
        [{"type": "price_above_cost"}],
        cost_lookup=lambda ch, sku: None,
    )
    # 无平台真相，回退模型传的 50 → 30<50 拦截
    assert gate("update_price", {"new_price": 30, "cost_price": 50}).action == PreToolUseAction.BLOCK
    # 回退且模型也没传 → cost 0 → 放行
    assert gate("update_price", {"new_price": 30}).action == PreToolUseAction.ALLOW