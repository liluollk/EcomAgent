"""期望轨迹断言器 — 对单步对话事件流做契约匹配。

与引擎层测试共用同一套规则（tests/e2e 驱动器从本模块复用）：
  1. 渐进式加载第一步：load_skill 元调用必须出现；
  2. 业务工具：tool_start 的工具名与参数子集逐项匹配；
  3. 结果：tool_result 的 is_error 与文本 token 匹配；
  4. 重试契约：step.attempts 断言 Execution Policy 的实际尝试次数；
  5. 权限契约：step.permission 断言 ASK 流程（请求事件 + 批准/拒绝）；
  6. 收尾：以 complete 事件结束。
"""

from __future__ import annotations

from events.agent_event import ToolResultEvent, ToolStartEvent

_STEP_MISSING_MSG = "[{name}] step「{msg}」期望工具 {tool}，实际 {actual}"
_STEP_INPUT_MSG = "[{name}] step「{msg}」参数 {k}={actual} 期望 {v}"
_STEP_ERROR_MSG = "[{name}] step「{msg}」期望失败结果，实际成功"
_STEP_OK_MSG = "[{name}] step「{msg}」工具执行失败: {result}"
_STEP_TOKEN_MSG = "[{name}] step「{msg}」结果文本缺「{token}」: {result}"
_STEP_COMPLETE_MSG = "[{name}] step「{msg}」未以 complete 收尾"
_STEP_ATTEMPTS_MSG = "[{name}] step「{msg}」重试次数 期望 {want}，实际 {actual}"
_STEP_PERM_MSG = "[{name}] step「{msg}」未发起权限请求（ASK 流程缺失）"


def has_load_skill(events: list) -> bool:
    """断言渐进式加载的第一步：load_skill 元调用出现过。"""
    return any(isinstance(e, ToolStartEvent) and e.tool_name == "load_skill" for e in events)


def first_tool_start(events: list) -> ToolStartEvent:
    """返回第一个业务工具调用（跳过渐进式加载的 load_skill 元调用）。"""
    for ev in events:
        if isinstance(ev, ToolStartEvent) and ev.tool_name != "load_skill":
            return ev
    raise AssertionError(f"未发现业务 tool_start 事件: {[e.type for e in events]}")


def tool_results(events: list) -> list[ToolResultEvent]:
    return [e for e in events if isinstance(e, ToolResultEvent) and e.tool_name != "load_skill"]


def assert_step(scenario_name: str, step: dict, events: list, relaxed: bool = False) -> None:
    """对单步对话事件流做契约断言（期望轨迹匹配）。

    Args:
        scenario_name: 场景名（错误消息定位用）。
        step: 场景表中的一个 step 字典（字段约定见 harness/cases.py）。
        events: 单轮对话收集到的 AgentEvent 流。
        relaxed: 真实模型模式——只断言运行时契约（工具名/结果语义/重试/
            权限/收尾），跳过决策契约（load_skill 前置、入参子集精确匹配）。
            剧本回归模式 relaxed=False，两类契约全查。

    Raises:
        AssertionError: 任一契约点不满足。
    """
    if not relaxed:
        assert has_load_skill(events), f"[{scenario_name}] step「{step['message']}」未出现 load_skill（渐进式加载第一步）"

    if step.get("expect_tool", True):
        ts = first_tool_start(events)
        assert ts.tool_name == step["tool"], _STEP_MISSING_MSG.format(
            name=scenario_name, msg=step["message"], tool=step["tool"], actual=ts.tool_name
        )
        if not relaxed:
            for k, v in step.get("input", {}).items():
                assert ts.input.get(k) == v, _STEP_INPUT_MSG.format(
                    name=scenario_name, msg=step["message"], k=k, actual=ts.input.get(k), v=v
                )

    results = tool_results(events)
    assert len(results) >= 1, f"[{scenario_name}] step「{step['message']}」未产生 tool_result"
    if step.get("result_is_error"):
        assert results[0].is_error, _STEP_ERROR_MSG.format(name=scenario_name, msg=step["message"])
    else:
        assert not results[0].is_error, _STEP_OK_MSG.format(
            name=scenario_name, msg=step["message"], result=results[0].result
        )
    # 文本 token 匹配对成功/失败结果一视同仁（平台错误文本走成功结果通道）
    for token in step.get("result_contains", []):
        assert token in results[0].result, _STEP_TOKEN_MSG.format(
            name=scenario_name, msg=step["message"], token=token, result=results[0].result
        )

    attempts = step.get("attempts")
    if attempts is not None:
        actual = getattr(results[0], "attempt", 1)
        assert actual == attempts, _STEP_ATTEMPTS_MSG.format(
            name=scenario_name, msg=step["message"], want=attempts, actual=actual
        )

    if step.get("permission"):
        assert any(e.type == "permission_request" for e in events), _STEP_PERM_MSG.format(
            name=scenario_name, msg=step["message"]
        )

    assert any(e.type == "complete" for e in events), _STEP_COMPLETE_MSG.format(
        name=scenario_name, msg=step["message"]
    )
