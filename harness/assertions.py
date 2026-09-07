"""期望轨迹断言器 — 对单步对话事件流做契约匹配。

与引擎层测试共用同一套规则（tests/e2e 驱动器从本模块复用）：
  1. 渐进式加载第一步：load_skill 元调用必须出现（仅剧本严格模式）；
  2. 业务工具：剧本模式要求第一个业务工具即期望工具、入参子集逐项匹配；
     real 模式只要求期望工具出现在业务序列中（先读后写不判负，见 relaxed）；
  3. 结果：期望工具自身 tool_result 的 is_error 与文本 token 匹配；
  4. 重试契约：step.attempts 断言 Execution Policy 的实际尝试次数；
  5. 权限契约：step.permission 断言 ASK 流程（请求事件 + 批准/拒绝）；
  6. 收尾：以 complete 事件结束。

real 模式另有 not exercised 三态：期望工具从未被触发抛 NotExercisedError，
由运行器归类为 not_exercised（不计入失败、不计入通过率分母）。
"""

from __future__ import annotations

from typing import Optional

from events.agent_event import ToolResultEvent, ToolStartEvent

_STEP_MISSING_MSG = "[{name}] step「{msg}」期望工具 {tool}，实际 {actual}"
_STEP_NOT_EXERCISED_MSG = "[{name}] step「{msg}」期望业务工具 {tool} 未被模型触发（not exercised，不计入失败）"
_STEP_INPUT_MSG = "[{name}] step「{msg}」参数 {k}={actual} 期望 {v}"
_STEP_ERROR_MSG = "[{name}] step「{msg}」期望失败结果，实际成功"
_STEP_OK_MSG = "[{name}] step「{msg}」工具执行失败: {result}"
_STEP_TOKEN_MSG = "[{name}] step「{msg}」结果文本缺「{token}」: {result}"
_STEP_COMPLETE_MSG = "[{name}] step「{msg}」未以 complete 收尾"
_STEP_ATTEMPTS_MSG = "[{name}] step「{msg}」重试次数 期望 {want}，实际 {actual}"
_STEP_PERM_MSG = "[{name}] step「{msg}」未发起权限请求（ASK 流程缺失）"


class NotExercisedError(AssertionError):
    """真实模型模式下，期望的业务工具未被模型触发（not exercised）。

    与断言失败不同义：模型未造出「写尝试」前提 ≠ 运行时守门/重试契约
    被破坏。真实评测统计时从分母剔除（契约未被触发，不构成对运行时的
    检验），而不是当作失败噪声。
    """


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


def match_tool_start(events: list, tool: str) -> Optional[ToolStartEvent]:
    """业务序列中首次出现指定工具的 start 事件；未触发返回 None。

    real 模式契约口径：期望工具「出现在业务序列中即可」——真实模型先读
    后写（如按 SKILL 建议先 query_inventory 再调价）是合理行为，不被判负。
    """
    for ev in events:
        if isinstance(ev, ToolStartEvent) and ev.tool_name == tool:
            return ev
    return None


def match_tool_results(events: list, tool: str) -> list[ToolResultEvent]:
    """属于指定业务工具的结果事件（重试只发最终结果，一次调用至多一条）。"""
    return [e for e in events if isinstance(e, ToolResultEvent) and e.tool_name == tool]


def assert_step(scenario_name: str, step: dict, events: list, relaxed: bool = False) -> None:
    """对单步对话事件流做契约断言（期望轨迹匹配）。

    Args:
        scenario_name: 场景名（错误消息定位用）。
        step: 场景表中的一个 step 字典（字段约定见 harness/cases.py）。
        events: 单轮对话收集到的 AgentEvent 流。
        relaxed: 真实模型模式——只断言运行时契约（工具名出现在业务序列/
            结果语义/重试/权限/收尾），跳过决策契约（load_skill 前置、
            首工具次序、入参子集精确匹配）。剧本回归模式 relaxed=False，
            两类契约全查。

    Raises:
        NotExercisedError: relaxed 模式下期望工具未被模型触发。
        AssertionError: 任一契约点不满足。
    """
    if not relaxed:
        assert has_load_skill(events), f"[{scenario_name}] step「{step['message']}」未出现 load_skill（渐进式加载第一步）"

    if step.get("expect_tool", True):
        if relaxed:
            # real 模式契约口径：期望工具出现在业务序列中即可（先读后写不判负）
            ts = match_tool_start(events, step["tool"])
            if ts is None:
                raise NotExercisedError(_STEP_NOT_EXERCISED_MSG.format(
                    name=scenario_name, msg=step["message"], tool=step["tool"]))
        else:
            ts = first_tool_start(events)
        assert ts.tool_name == step["tool"], _STEP_MISSING_MSG.format(
            name=scenario_name, msg=step["message"], tool=step["tool"], actual=ts.tool_name
        )
        if not relaxed:
            for k, v in step.get("input", {}).items():
                assert ts.input.get(k) == v, _STEP_INPUT_MSG.format(
                    name=scenario_name, msg=step["message"], k=k, actual=ts.input.get(k), v=v
                )

    # 工具结果契约仅在期望业务工具时断言；expect_tool=False 的纯文本轮次
    # （如记忆陈述）真实模型只文本回应、不调工具，属正常行为
    if step.get("expect_tool", True):
        if relaxed:
            # 结果断言归属到期望工具自身（模型可能先做了其他只读调用）
            results = match_tool_results(events, step["tool"])
            assert len(results) >= 1, (
                f"[{scenario_name}] step「{step['message']}」期望工具 {step['tool']} "
                "已发起调用但未产生 tool_result"
            )
        else:
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
