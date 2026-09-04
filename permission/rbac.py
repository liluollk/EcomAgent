"""
RBAC — 基于角色的访问控制（身份门）。

多角色场景：电商运营团队的 store owner / operator / customer_service / finance
各自持有不同写权限；读操作（query_/get_/list_/search_ 前缀）对所有角色开放。

身份门注册为 PreToolUse 管线第一位检查器：先校验“该角色是否有权执行此工具”，
再交给模式门 / 业务规则 / ASK 继续做行为合规判断。职责分离：
- 身份门（本模块）：角色 → 工具级 ACL
- 行为门（rule_engine）：模式语义 + 业务参数规则
- 人工确认（base_agent）：ASK 挂起
- 审计：permission_requests / tool_calls 记录带 user 字段，形成“谁在何时做了什么”追责链

与管理员概念的差异：本模块是“角色维度的工具授权”，不引入“管理员”实体。
"""

from __future__ import annotations

from typing import Callable

from .pre_tool_use import PreToolUseResult, PreToolUseAction

# ---------------------------------------------------------------------------
# 角色定义（真实电商运营场景）
# ---------------------------------------------------------------------------

MANAGER = "manager"            # 店长：全权限（商品 / 促销 / 上下架）
OPERATOR = "operator"          # 运营专员：商品改价、促销活动
CUSTOMER_SERVICE = "customer_service"  # 客服：订单查询、售后工单（仅读商品域）
FINANCE = "finance"            # 财务：只读（库存 / 订单 / 价格）

ALL_ROLES = frozenset({
    MANAGER,
    OPERATOR,
    CUSTOMER_SERVICE,
    FINANCE,
})

# 角色显示名（前端身份选择器 / 审计展示用）
ROLE_LABELS = {
    MANAGER: "店长",
    OPERATOR: "运营专员",
    CUSTOMER_SERVICE: "客服",
    FINANCE: "财务",
}

DEFAULT_ROLE = OPERATOR

_READ_PREFIXES = ("query_", "get_", "list_", "search_")

# 角色 → 允许调用的写工具集合（读工具对所有角色开放）。
# 批次 4 扩展：商品上下架（product_shelf）与售后工单（service_ticket）入 ACL：
#   manager 店长全权（改价 / 促销 / 上下架 / 工单）；
#   operator 运营（改价 / 促销 / 上下架，不越权开售后工单）；
#   customer_service 客服（售后工单，商品域只读，不越权改价 / 上下架）；
#   finance 财务只读。
ROLE_WRITE_ACL: dict[str, frozenset[str]] = {
    MANAGER: frozenset({"update_price", "create_promotion", "product_shelf", "service_ticket"}),
    OPERATOR: frozenset({"update_price", "create_promotion", "product_shelf"}),
    CUSTOMER_SERVICE: frozenset({"service_ticket"}),
    FINANCE: frozenset(),
}


def is_valid_role(role: str) -> bool:
    """判断角色名是否合法。"""
    return role in ALL_ROLES


def can_role_write(role: str, tool_name: str) -> bool:
    """角色是否被授权调用指定写工具（读工具视为全放开）。

    Args:
        role: 角色名。
        tool_name: 工具名。

    Returns:
        bool: 读操作恒为 True；写操作查询 ACL（未知角色按只读处理）。
    """
    if tool_name.startswith(_READ_PREFIXES):
        return True
    # load_skill 是元工具（仅加载技能上下文，不产生外部副作用），所有角色可用
    if tool_name == "load_skill":
        return True
    allowed = ROLE_WRITE_ACL.get(role)
    if allowed is None:
        # 未知角色安全默认：只读
        return False
    return tool_name in allowed


def role_gate_rule(role: str) -> Callable:
    """角色门规则工厂 — 将角色 ACL 接入 PreToolUse 管线。

    放在管线第一位：身份门最先执行，不通过直接 BLOCK，
    不进入后续模式门 / 业务规则 / ASK 判断。

    Args:
        role: 会话绑定的角色名（来自 session.user["role"]）。

    Returns:
        Callable: 检查器函数 (tool_name, tool_input) -> PreToolUseResult。
    """

    def _gate(tool_name: str, tool_input: dict) -> PreToolUseResult:
        if can_role_write(role, tool_name):
            return PreToolUseResult(action=PreToolUseAction.ALLOW)
        return PreToolUseResult(
            action=PreToolUseAction.BLOCK,
            reason=(
                f"角色 {ROLE_LABELS.get(role, role)} 无权执行写操作 {tool_name}"
            ),
        )

    return _gate