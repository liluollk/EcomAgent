"""统一工具安全策略类型与内置工具策略注册表。

工具集已收敛为「默认调价闭环 + 元技能」与「扩展边界能力」两层：

- 默认工具集（DEFAULT）：query_product_snapshot（读）/ update_price（写，需审批）
  / save_skill（写，需审批）/ load_skill（读，元技能，由 Agent 自带，不入
  builtin_tools.get_definitions 但保留策略供一致性测试）。
- 扩展工具集（EXTENSION）：原 11 个操作里其余的客服 / 知识库 / 经营分析 /
  促销 / 上下架等能力，不参与默认 Agent，仅在显式注册扩展通道后可见。

get_builtin_tool_policies() 仍返回全量（默认 ∪ 扩展）以保证 rbac / rule_engine
的历史调用不破坏（身份门按工具名查 ACL 时仍能找到扩展写工具）。
"""

from __future__ import annotations

from typing import Literal, TypedDict


class ToolPolicy(TypedDict):
    """工具治理元数据，不暴露给模型的 function schema。"""

    side_effect: Literal["read", "write"]
    requires_approval: bool


# 默认工具集（默认 Agent 可见的调价闭环 + 元技能）
_DEFAULT_TOOL_POLICIES: dict[str, ToolPolicy] = {
    "load_skill": {"side_effect": "read", "requires_approval": False},
    "query_product_snapshot": {"side_effect": "read", "requires_approval": False},
    "update_price": {"side_effect": "write", "requires_approval": True},
    "save_skill": {"side_effect": "write", "requires_approval": True},
}

# 扩展工具集（不参与默认 Agent，需显式注册扩展通道后才可见）
_EXTENSION_TOOL_POLICIES: dict[str, ToolPolicy] = {
    "query_inventory": {"side_effect": "read", "requires_approval": False},
    "create_promotion": {"side_effect": "write", "requires_approval": True},
    "query_order_status": {"side_effect": "read", "requires_approval": False},
    "product_shelf": {"side_effect": "write", "requires_approval": True},
    "service_ticket": {"side_effect": "write", "requires_approval": True},
    "query_order_stats": {"side_effect": "read", "requires_approval": False},
    "query_anomalies": {"side_effect": "read", "requires_approval": False},
    "query_promotions": {"side_effect": "read", "requires_approval": False},
    "query_after_sales_stats": {"side_effect": "read", "requires_approval": False},
    "query_knowledge_base": {"side_effect": "read", "requires_approval": False},
}

# 兼容层：rbac / rule_engine 历史依赖全量策略
_ALL_TOOL_POLICIES: dict[str, ToolPolicy] = {
    **_DEFAULT_TOOL_POLICIES,
    **_EXTENSION_TOOL_POLICIES,
}


def get_default_tool_policies() -> dict[str, ToolPolicy]:
    """返回默认工具策略副本（默认 Agent 治理用），避免调用方修改全局注册表。"""
    return {name: dict(policy) for name, policy in _DEFAULT_TOOL_POLICIES.items()}


def get_extension_tool_policies() -> dict[str, ToolPolicy]:
    """返回扩展工具策略副本（扩展通道治理用），避免调用方修改全局注册表。"""
    return {name: dict(policy) for name, policy in _EXTENSION_TOOL_POLICIES.items()}


def get_builtin_tool_policies() -> dict[str, ToolPolicy]:
    """返回内置工具全量策略副本（默认 ∪ 扩展），保持 rbac / rule_engine 兼容。"""
    return {name: dict(policy) for name, policy in _ALL_TOOL_POLICIES.items()}


def policy_from_definition(definition: dict) -> ToolPolicy | None:
    """从外部工具定义读取显式策略；没有声明时返回 None。"""
    raw = definition.get("tool_policy") or definition.get("x-tool-policy")
    if not isinstance(raw, dict):
        return None
    side_effect = raw.get("side_effect")
    requires_approval = raw.get("requires_approval")
    if side_effect not in {"read", "write"} or not isinstance(requires_approval, bool):
        return None
    return {"side_effect": side_effect, "requires_approval": requires_approval}
