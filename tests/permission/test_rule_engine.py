"""测试业务规则引擎 — 价格成本保护规则。"""

import pytest
from permission.rule_engine import price_above_cost_rule, mode_gate_rule
from permission.pre_tool_use import PreToolUseAction


def test_price_above_cost_allows_price_increase():
    result = price_above_cost_rule(
        "update_price",
        {"new_price": 100, "cost_price": 50},
    )
    assert result.action == PreToolUseAction.ALLOW


def test_price_above_cost_blocks_price_below_cost():
    result = price_above_cost_rule(
        "update_price",
        {"new_price": 30, "cost_price": 50},
    )
    assert result.action == PreToolUseAction.BLOCK
    assert "低于成本价" in result.reason


def test_price_above_cost_ignores_other_tools():
    result = price_above_cost_rule(
        "query_inventory",
        {"channel": "taobao"},
    )
    assert result.action == PreToolUseAction.ALLOW


def test_mode_gate_static_ask_blocks_write():
    gate = mode_gate_rule("ASK")
    assert gate("query_inventory", {}).action == PreToolUseAction.ALLOW
    assert gate("update_price", {}).action == PreToolUseAction.ASK


def test_mode_gate_static_readonly_blocks_write():
    gate = mode_gate_rule("READONLY")
    assert gate("query_inventory", {}).action == PreToolUseAction.ALLOW
    assert gate("update_price", {}).action == PreToolUseAction.BLOCK


def test_mode_gate_static_execute_allows_write():
    gate = mode_gate_rule("EXECUTE")
    assert gate("update_price", {}).action == PreToolUseAction.ALLOW


def test_mode_gate_callable_dynamic_switch():
    """传入 callable 时每次调用实时取值，切换即时生效（同一 agent 管线无需重建）。"""
    current = {"mode": "ASK"}
    gate = mode_gate_rule(lambda: current["mode"])
    assert gate("update_price", {}).action == PreToolUseAction.ASK
    current["mode"] = "EXECUTE"
    assert gate("update_price", {}).action == PreToolUseAction.ALLOW
    current["mode"] = "READONLY"
    assert gate("update_price", {}).action == PreToolUseAction.BLOCK