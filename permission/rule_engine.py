"""
业务规则引擎 — 电商场景专用的 PreToolUse 检查器。

可插拔的检查器函数。
每条规则是一个独立的检查器函数，可注册到 PreToolUsePipeline 中。
"""

from __future__ import annotations

from typing import Any

from .pre_tool_use import PreToolUseResult, PreToolUseAction


def price_above_cost_rule(
    tool_name: str, tool_input: dict[str, Any]
) -> PreToolUseResult:
    """价格不低于成本价规则。

    拦截 update_price 工具调用中 new_price 低于 cost_price 的情况。
    确保运营人员不会将价格设为低于成本价。

    Args:
        tool_name: 工具名称。
        tool_input: 工具参数，应包含 new_price 和 cost_price。

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


def mode_gate_rule(mode):
    """模式门规则工厂 — 将会话权限模式接入 PreToolUse 管线。

    对应 pre_tool_use 文档中描述的模式语义：
    - READONLY: 非只读工具（非 query_/get_/list_/search_ 前缀）自动 BLOCK。
    - ASK: 非只读工具触发 ASK，挂起等待用户确认。
    - EXECUTE: 全部放行（除非被其他业务规则拦截）。

    Args:
        mode: 权限模式名（READONLY / ASK / EXECUTE），或返回模式名的可调用对象。
            传 callable 时每次调用实时取值，实现「同一条 WS 连接内切换即时生效」。

    Returns:
        Callable: 检查器函数，可注册到 PreToolUsePipeline。
    """

    def _gate(tool_name: str, tool_input: dict[str, Any]) -> PreToolUseResult:
        current = mode() if callable(mode) else mode
        # load_skill 是元工具（仅加载技能上下文，不产生外部副作用），视为只读放行
        is_read = tool_name == "load_skill" or tool_name.startswith(("query_", "get_", "list_", "search_"))
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


def workspace_rules_rule(rules: list[dict]):
    """工作区业务规则工厂 — 将 Workspace.rules 声明注册为 PreToolUse 检查器。

    每项声明 {"type": "price_above_cost", ...} 分派到实现函数；
    任一规则返回非 ALLOW 即短路（与管线整体短路语义一致）；
    未知规则类型与空规则列表视为全放行。

    Args:
        rules: Workspace.rules 列表（业务规则声明）。

    Returns:
        Callable: 检查器函数 (tool_name, tool_input) -> PreToolUseResult。
    """

    def _gate(tool_name: str, tool_input: dict[str, Any]) -> PreToolUseResult:
        for rule in rules or []:
            impl = _RULE_DISPATCH.get(rule.get("type", ""))
            if impl is None:
                continue
            result = impl(tool_name, tool_input)
            if result.action != PreToolUseAction.ALLOW:
                return result
        return PreToolUseResult(action=PreToolUseAction.ALLOW)

    return _gate
