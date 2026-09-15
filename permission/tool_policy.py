"""统一工具安全策略类型与内置工具策略注册表。"""

from __future__ import annotations

from typing import Literal, TypedDict


class ToolPolicy(TypedDict):
    """工具治理元数据，不暴露给模型的 function schema。"""

    side_effect: Literal["read", "write"]
    requires_approval: bool


_BUILTIN_TOOL_POLICIES: dict[str, ToolPolicy] = {
    "load_skill": {"side_effect": "read", "requires_approval": False},
    "query_inventory": {"side_effect": "read", "requires_approval": False},
    "update_price": {"side_effect": "write", "requires_approval": True},
    "create_promotion": {"side_effect": "write", "requires_approval": True},
    "query_order_status": {"side_effect": "read", "requires_approval": False},
    "product_shelf": {"side_effect": "write", "requires_approval": True},
    "service_ticket": {"side_effect": "write", "requires_approval": True},
    "query_order_stats": {"side_effect": "read", "requires_approval": False},
    "query_anomalies": {"side_effect": "read", "requires_approval": False},
    "query_promotions": {"side_effect": "read", "requires_approval": False},
    "query_after_sales_stats": {"side_effect": "read", "requires_approval": False},
    "query_knowledge_base": {"side_effect": "read", "requires_approval": False},
    "save_skill": {"side_effect": "write", "requires_approval": True},
}


def get_builtin_tool_policies() -> dict[str, ToolPolicy]:
    """返回内置工具策略副本，避免调用方修改全局注册表。"""
    return {name: dict(policy) for name, policy in _BUILTIN_TOOL_POLICIES.items()}


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
