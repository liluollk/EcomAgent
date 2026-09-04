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


def test_read_tools_open_to_all_roles():
    """读工具（query_/get_/list_/search_ 前缀）对所有角色放行。"""
    for role in ALL_ROLES:
        for tool in ("query_inventory", "get_order", "list_products", "search_products"):
            assert can_role_write(role, tool), f"{role} 应可读 {tool}"
            gate = role_gate_rule(role)
            assert gate(tool, {}).action == PreToolUseAction.ALLOW


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


def test_unknown_role_read_only_safe_default():
    """未知角色按只读处理：读放行、写拦截。"""
    assert not can_role_write("admin", "update_price")
    assert can_role_write("admin", "query_inventory")
    gate = role_gate_rule("admin")
    assert gate("update_price", {}).action == PreToolUseAction.BLOCK
    assert gate("query_inventory", {}).action == PreToolUseAction.ALLOW