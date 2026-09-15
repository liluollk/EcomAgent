"""RBAC 身份门测试 — 多角色 × 工具 ACL 矩阵、角色合法性、读操作全放开。"""

from permission.rbac import (
    can_role_write,
    is_valid_role,
    role_gate_rule,
    ALL_ROLES,
    MANAGER,
    OPERATOR,
    CUSTOMER_SERVICE,
    FINANCE,
    DEFAULT_ROLE,
)
from permission.pre_tool_use import PreToolUseAction


def test_role_set_and_default():
    """角色集应包含四个电商运营角色，默认角色为运营。"""
    assert ALL_ROLES == {MANAGER, OPERATOR, CUSTOMER_SERVICE, FINANCE}
    assert DEFAULT_ROLE == OPERATOR
    assert is_valid_role("manager")
    assert not is_valid_role("admin")  # 管理员概念不存在


def test_explicit_read_tools_open_to_all_roles():
    """注册为 read 的工具对所有角色放行，名称前缀本身不授予权限。"""
    policies = {
        "query_inventory": {"side_effect": "read", "requires_approval": False},
    }
    for role in ALL_ROLES:
        assert can_role_write(role, "query_inventory", policies.get)
        gate = role_gate_rule(role, policies.get)
        assert gate("query_inventory", {}).action == PreToolUseAction.ALLOW


def test_unknown_prefixed_tool_is_not_read_by_name():
    for role in ALL_ROLES:
        assert not can_role_write(role, "query_unknown", lambda _name: None)
        assert role_gate_rule(role, lambda _name: None)("get_unknown", {}).action == PreToolUseAction.BLOCK


def test_manager_full_write():
    """店长可执行全部写工具（改价、促销）。"""
    for tool in ("update_price", "create_promotion"):
        assert can_role_write(MANAGER, tool)


def test_operator_write_allowed():
    """运营可改价与建促销（商品域写操作）。"""
    assert can_role_write(OPERATOR, "update_price")
    assert can_role_write(OPERATOR, "create_promotion")


def test_customer_service_write_blocked():
    """客服不可改价 / 建促销（仅可读），身份门直接 BLOCK。"""
    assert not can_role_write(CUSTOMER_SERVICE, "update_price")
    assert not can_role_write(CUSTOMER_SERVICE, "create_promotion")
    gate = role_gate_rule(CUSTOMER_SERVICE)
    result = gate("update_price", {"new_price": 100})
    assert result.action == PreToolUseAction.BLOCK
    assert "客服" in result.reason


def test_finance_read_only():
    """财务只读：任何写操作被身份门拦截。"""
    for tool in ("update_price", "create_promotion"):
        assert not can_role_write(FINANCE, tool)
    gate = role_gate_rule(FINANCE)
    assert gate("update_price", {}).action == PreToolUseAction.BLOCK
    assert gate("query_inventory", {}).action == PreToolUseAction.ALLOW


def test_unknown_role_safe_default():
    """未知角色和未知工具均安全默认拒绝。"""
    assert not can_role_write("admin", "update_price")
    assert not can_role_write("admin", "query_inventory", lambda _name: None)
    gate = role_gate_rule("admin")
    assert gate("update_price", {}).action == PreToolUseAction.BLOCK
    assert gate("query_inventory", {}).action == PreToolUseAction.BLOCK
