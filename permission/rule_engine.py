"""
业务规则引擎 — 电商场景专用的 PreToolUse 检查器。

可插拔的检查器函数。
每条规则是一个独立的检查器函数，可注册到 PreToolUsePipeline 中。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .pre_tool_use import PreToolUseResult, PreToolUseAction
from .tool_policy import get_builtin_tool_policies


def price_above_cost_rule(
    tool_name: str, tool_input: dict[str, Any]
) -> PreToolUseResult:
    """价格不低于成本价规则。

    拦截 update_price 工具调用中 new_price 低于有效 cost_price 的情况。
    成本价来源：生产装配经 workspace_rules_rule 注入平台真相 cost_lookup，
    覆盖 tool_input["cost_price"]（成本保护由平台数据决定，不由模型决定）；
    无注入时保留直接调用方的低层规则语义；平台装配时必须注入
    CostProvider，不能让模型参数作为平台成本真相。

    Args:
        tool_name: 工具名称。
        tool_input: 规则参数，应包含 new_price；cost_price 由工作区规则装配的平台成本真相提供。

    Returns:
        PreToolUseResult: 价格合规则 ALLOW，否则 BLOCK。
    """
    if tool_name != "update_price":
        return PreToolUseResult(action=PreToolUseAction.ALLOW)

    new_price = tool_input.get("new_price", 0)
    cost_price = tool_input.get("cost_price", 0)

    if new_price < cost_price:
        return PreToolUseResult(
            action=PreToolUseAction.BLOCK,
            reason=f"新价格 {new_price} 低于成本价 {cost_price}，不允许调整",
        )
    return PreToolUseResult(action=PreToolUseAction.ALLOW)


def mode_gate_rule(
    mode,
    policy_lookup: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
):
    """模式门规则工厂 — 将会话权限模式接入 PreToolUse 管线。

    对应 pre_tool_use 文档中描述的模式语义：
    - READONLY: 写工具自动 BLOCK，显式只读工具 ALLOW。
    - ASK: 需要审批的工具触发 ASK，挂起等待用户确认。
    - EXECUTE: 全部放行（除非被其他业务规则拦截）。

    Args:
        mode: 权限模式名（READONLY / ASK / EXECUTE），或返回模式名的可调用对象。
            传 callable 时每次调用实时取值，实现「同一条 WS 连接内切换即时生效」。
        policy_lookup: 按工具名返回安全策略的函数。未找到策略时拒绝执行，
            从而保证未知工具不能通过 EXECUTE 或命名约定绕过治理。

    Returns:
        Callable: 检查器函数，可注册到 PreToolUsePipeline。
    """

    lookup = policy_lookup or get_builtin_tool_policies().get

    def _gate(tool_name: str, tool_input: dict[str, Any]) -> PreToolUseResult:
        current = mode() if callable(mode) else mode
        policy = lookup(tool_name)
        if policy is None:
            return PreToolUseResult(
                action=PreToolUseAction.BLOCK,
                reason=f"工具 {tool_name} 未注册安全策略，默认拒绝执行",
            )
        policy = policy or {}
        side_effect = str(policy.get("side_effect", "write")).lower()
        requires_approval = bool(policy.get("requires_approval", side_effect != "read"))
        is_read = side_effect == "read" and not requires_approval

        if is_read or current == "EXECUTE":
            return PreToolUseResult(action=PreToolUseAction.ALLOW)
        if current == "READONLY":
            return PreToolUseResult(
                action=PreToolUseAction.BLOCK,
                reason=f"当前为只读模式，写操作 {tool_name} 已被拦截",
            )
        # ASK 模式
        return PreToolUseResult(
            action=PreToolUseAction.ASK,
            reason=f"写操作 {tool_name} 需要用户确认后执行",
        )

    return _gate


# ---------------------------------------------------------------------------
# Workspace 业务规则 — Workspace.rules 声明接入 PreToolUse 管线
# ---------------------------------------------------------------------------

_RULE_DISPATCH: dict[str, Callable] = {
    "price_above_cost": price_above_cost_rule,
}


def workspace_rules_rule(rules: list[dict], cost_lookup: Optional[Callable[[Any, Any], Any]] = None):
    """工作区业务规则工厂 — 将 Workspace.rules 声明注册为 PreToolUse 检查器。

    每项声明 {"type": "price_above_cost", ...} 分派到实现函数；
    任一规则返回非 ALLOW 即短路（与管线整体短路语义一致）；
    未知规则类型由 Workspace API 在配置入口拒绝；执行过程中对未知声明跳过，
    以兼容直接构造的历史 Workspace 对象。

    cost_lookup（可选）：平台成本真相访问器 (channel, sku) -> cost_price | None。
    提供时，price_above_cost 规则以平台数据覆盖调用方传入的 cost_price——
    成本保护由平台真相决定，不由模型决定；返回 None 或抛错都会失败关闭。
    未注入时自动使用默认 CostProvider，绝不回退到模型传入的 cost_price。

    Args:
        rules: Workspace.rules 列表（业务规则声明）。
        cost_lookup: 平台成本真相查询回调（装配层注入，规则层不感知数据来源）。

    Returns:
        Callable: 检查器函数 (tool_name, tool_input) -> PreToolUseResult。
    """

    if cost_lookup is None:
        from integrations.commerce.cost_provider import get_cost_provider

        cost_lookup = get_cost_provider().get_cost_price

    def _gate(tool_name: str, tool_input: dict[str, Any]) -> PreToolUseResult:
        for rule in rules or []:
            rtype = rule.get("type", "")
            if rule.get("enabled") is False:
                continue
            impl = _RULE_DISPATCH.get(rtype)
            if impl is None:
                continue
            effective = tool_input
            if rtype == "price_above_cost" and tool_name == "update_price":
                # update_price 有两种入参形态：扩展工具集的历史形态
                # （channel / sku / new_price）与默认调价闭环的形态
                # （platform / product_id / sku_id / target_price）。
                # 早闸必须在两种形态下都能取到「平台 + SKU」与「目标价」，
                # 否则默认工具会因为取不到 SKU 而拿不到成本价，被失败关闭规则
                # 一律拦成「无法确认平台成本价」——那样成本保护就变成了全量拦截。
                # 成本真相始终来自 cost_lookup（平台数据），不回退到调用方传值。
                eff_platform = tool_input.get("platform") or tool_input.get("channel")
                eff_sku = tool_input.get("sku_id") or tool_input.get("sku")
                eff_price = tool_input.get("target_price", tool_input.get("new_price", 0))
                try:
                    platform_cost = cost_lookup(eff_platform, eff_sku)
                except Exception as exc:
                    return PreToolUseResult(
                        action=PreToolUseAction.BLOCK,
                        reason=f"无法获取平台成本价，已拦截调价：{exc}",
                    )
                if platform_cost is None:
                    return PreToolUseResult(
                        action=PreToolUseAction.BLOCK,
                        reason="无法确认平台成本价，已拦截调价",
                    )
                effective = {**tool_input, "new_price": eff_price, "cost_price": platform_cost}
            result = impl(tool_name, effective)
            if result.action != PreToolUseAction.ALLOW:
                return result
        return PreToolUseResult(action=PreToolUseAction.ALLOW)

    return _gate
