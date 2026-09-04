"""并发工具执行测试 — asyncio.gather 并发调度：结果归位、耗时、abort 取消、审计、拦截隔离。"""

import asyncio
import time

from agent_backend.protocol import AgentCapabilities, BackendConfig, BackendProvider
from agent_backend.mock_backend import MockAgent  # noqa: F401 (保留对齐)
from agent_runtime.base_agent import BaseAgent
from events.agent_event import ToolStartEvent, TextDeltaEvent
from permission.pre_tool_use import PreToolUseAction, PreToolUsePipeline, PreToolUseResult
from session.session import Session, PermissionMode
from session.workspace import Workspace


class MultiToolBackend:
    """脚本化后端：第一轮产出多个 tool_start（无权限挂起场景），第二轮总结。"""

    def __init__(self, starts: list[tuple[str, str, dict]]) -> None:
        self.rounds = 0
        self._starts = starts

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(supports_tool_calling=True, supports_thinking_level=False,
                                 supports_json_mode=True, context_window=128000)

    def get_config(self) -> BackendConfig:
        return BackendConfig(provider=BackendProvider.MOCK, model="mock", api_key="t")

    def update_runtime_config(self, config: BackendConfig) -> None:
        pass

    def abort(self, reason: str) -> None:
        pass

    async def chat(self, messages, tools, session_id):
        self.rounds += 1
        if self.rounds == 1:
            for name, tid, inp in self._starts:
                yield ToolStartEvent(tool_name=name, tool_use_id=tid, input=inp)
        else:
            yield TextDeltaEvent(text="完成")


def _make_agent(backend, checkers=None):
    ws = Workspace(workspace_id="w1", name="t")
    session = Session(session_id="s1", workspace=ws, permission_mode=PermissionMode.ASK,
                      active_sources=["taobao"], user={"user_id": "u1", "role": "operator"})
    agent = BaseAgent(backend, ws)
    pipeline = PreToolUsePipeline()
    for c in checkers or []:
        pipeline.add_checker(c)
    agent.set_permission_pipeline(pipeline)
    return agent, session


def _collect(agent, session, text):
    """跑一轮 chat，返回 (tool_result 按 id 映射, 收集到的所有事件类型, 总耗时)。"""
    results = {}
    ev_types = []
    t0 = time.monotonic()

    async def scenario():
        nonlocal t0
        async for ev in agent.chat(session, text, [{"name": "t", "description": "", "parameters": {}}]):
            ev_types.append(ev.type)
            if ev.type == "tool_result":
                results[ev.tool_use_id] = ev
        return time.monotonic() - t0

    elapsed = asyncio.run(scenario())
    return results, ev_types, elapsed


def _async_result(label, wait):
    async def handler(**kw):
        await asyncio.sleep(wait)
        return label
    return handler


def test_concurrent_results_mapped_and_faster():
    """多个独立工具并发执行：结果按 tool_use_id 正确归位，且总耗时≈最慢（非串行和）。"""
    backend = MultiToolBackend([
        ("tool_a", "call_a", {"x": 1}),
        ("tool_b", "call_b", {"x": 2}),
        ("tool_c", "call_c", {"x": 3}),
    ])
    agent, session = _make_agent(backend)
    agent.set_tool_handlers({
        "tool_a": _async_result("A", 0.18),
        "tool_b": _async_result("B", 0.10),
        "tool_c": _async_result("C", 0.14),
    })
    results, ev_types, elapsed = _collect(agent, session, "并行执行三个工具")
    assert set(results) == {"call_a", "call_b", "call_c"}
    assert {k: r.result for k, r in results.items()} == {"call_a": "A", "call_b": "B", "call_c": "C"}
    # 串行耗时 0.42s；并发应 ≈ 最慢 0.18s（阈值宽松防 CI 抖动）
    assert elapsed < 0.32, f"并发未生效，耗时 {elapsed:.2f}s"
    # 无权限挂起事件（直通模式）
    assert "permission_request" not in ev_types


def test_concurrent_abort_cancels_remaining():
    """abort 后未完成工具被取消，不再产出结果；execution_state = ABORTED。"""
    backend = MultiToolBackend([
        ("fast", "call_f", {}),
        ("slow", "call_s", {}),
    ])
    agent, session = _make_agent(backend)
    saw_fast = []

    async def fast_handler(**kw):
        return "fast-ok"

    async def slow_handler(**kw):
        await asyncio.sleep(30)
        return "slow-ok"

    agent.set_tool_handlers({"fast": fast_handler, "slow": slow_handler})

    async def scenario():
        t0 = time.monotonic()
        results = {}
        async for ev in agent.chat(session, "并行", [{"name": "t", "description": "", "parameters": {}}]):
            if ev.type == "tool_result":
                results[ev.tool_use_id] = ev
                if ev.tool_use_id == "call_f":
                    agent.abort("用户中断")
        return results, time.monotonic() - t0

    results, elapsed = asyncio.run(scenario())
    assert "call_f" in results and "call_s" not in results
    # 慢任务被取消，不应等待 30s
    assert elapsed < 2.0, f"慢任务未被取消，耗时 {elapsed:.1f}s"
    assert session.execution_state.name == "ABORTED"


def test_concurrent_audit_records_all_tools():
    """多工具并发后，会话审计 tool_calls 记录数 == 工具数，且带 user/timestamp。"""
    backend = MultiToolBackend([
        ("t1", "call_1", {"a": 1}),
        ("t2", "call_2", {"a": 2}),
        ("t3", "call_3", {"a": 3}),
    ])
    agent, session = _make_agent(backend)
    agent.set_tool_handlers({
        "t1": lambda **kw: "r1",
        "t2": lambda **kw: "r2",
        "t3": lambda **kw: "r3",
    })
    _collect(agent, session, "并发审计")
    assert len(session.tool_calls) == 3
    names = {tc["tool_name"] for tc in session.tool_calls}
    assert names == {"t1", "t2", "t3"}
    for tc in session.tool_calls:
        assert tc["tool_use_id"] and tc["result"]
        assert tc["user"]["user_id"] == "u1" and tc["timestamp"]


def test_intercepted_tool_not_in_concurrent():
    """被拦截（BLOCK）的工具不进入并发执行；其余工具正常执行，同含拦截错误结果。"""
    def block_t2(tool_name, tool_input):
        if tool_name == "t2":
            return PreToolUseResult(action=PreToolUseAction.BLOCK, reason="业务规则拦截 t2")
        return PreToolUseResult(action=PreToolUseAction.ALLOW)

    backend = MultiToolBackend([
        ("t1", "call_1", {}),
        ("t2", "call_2", {}),
        ("t3", "call_3", {}),
    ])
    agent, session = _make_agent(backend, checkers=[block_t2])
    agent.set_tool_handlers({"t1": lambda **kw: "ok1", "t2": lambda **kw: "should-not-run", "t3": lambda **kw: "ok3"})
    results, ev_types, _ = _collect(agent, session, "并发含拦截")
    # t1/t3 执行成功
    assert results["call_1"].result == "ok1" and results["call_3"].result == "ok3"
    assert "call_1" in results
    # t2 未执行（无 handler 运行痕迹 → 结果被替换为拦截错误）
    assert "should-not-run" not in [r.result for r in results.values()]
    # 拦截错误结果存在（is_error=True，含 reason）
    blocked_results = [r for r in results.values() if r.is_error]
    assert len(blocked_results) == 1 and "拦截" in blocked_results[0].result
    # 审计里 t2 也记录了（拦截也算决策，但执行只 2 个）
    executed_ok = [tc for tc in session.tool_calls if not tc["is_error"]]
    assert {tc["tool_name"] for tc in executed_ok} == {"t1", "t3"}