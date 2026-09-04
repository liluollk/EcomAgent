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