"""执行循环对抗性评测（L1 对抗场景）— 真实模型必然产生的怪异输入。

覆盖 _execute_loop 契约的四类边界：
  1. 多工具并发：单轮 3 个 tool_start → 全部执行且消息按 tool_use_id 归位
  2. 乱序结果：并发任务先完成先 yield（FIRST_COMPLETED），组装仍按 ID 匹配
  3. 局部失败：单工具异常不拖垮整轮，其余工具照常执行
  4. 缺失参数：handler 缺参 TypeError → is_error=True 回传 LLM，不炸栈
"""

import asyncio

from agent_backend.protocol import BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import TextDeltaEvent, ToolStartEvent
from session.session import Session
from session.workspace import Workspace


class MultiToolBackend:
    """第一轮产出多个 tool_start，第二轮产出总结文本的脚本后端。"""

    def __init__(self, tool_starts: list[dict]) -> None:
        self.tool_starts = tool_starts
        self.rounds = 0

    def capabilities(self):
        return None

    def get_config(self) -> BackendConfig:
        return BackendConfig(provider=BackendProvider.OPENAI, model="mock", api_key="test")

    def update_runtime_config(self, config: BackendConfig) -> None:
        pass

    def abort(self, reason: str) -> None:
        pass

    async def chat(self, messages: list[dict], tools: list[dict], session_id: str):
        self.rounds += 1
        if self.rounds == 1:
            for spec in self.tool_starts:
                yield ToolStartEvent(
                    tool_name=spec["name"],
                    tool_use_id=spec["id"],
                    input=spec["input"],
                )
        else:
            yield TextDeltaEvent(text="全部处理完成")


def make_session() -> Session:
    workspace = Workspace(workspace_id="w1", name="test", metadata={"brand": "B"})
    return Session(session_id="s1", workspace=workspace, active_sources=["taobao"])


def run_until_complete(backend, handlers: dict, user_message: str = "批量处理") -> list:
    """跑完整 turn 并返回全部事件。"""

    async def collect() -> list:
        session = make_session()
        agent = BaseAgent(backend, session.workspace)
        agent.set_tool_handlers(handlers)
        return [ev async for ev in agent.chat(session, user_message, [])]

    return asyncio.run(collect())


def test_multi_tool_concurrent_execution():
    """单轮 3 个工具并发执行，全部结果按 tool_use_id 归位到消息结构。"""
    backend = MultiToolBackend([
        {"id": "call_a", "name": "query_inventory", "input": {"channel": "taobao", "sku": "A"}},
        {"id": "call_b", "name": "update_price", "input": {"channel": "taobao", "sku": "B", "new_price": 99}},
        {"id": "call_c", "name": "service_ticket", "input": {"channel": "taobao", "order_id": "T1"}},
    ])
    events = run_until_complete(backend, {
        "query_inventory": lambda **kw: "库存 10",
        "update_price": lambda **kw: "价格已更新",
        "service_ticket": lambda **kw: "工单已创建",
    })

    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 3, f"应产生 3 个 tool_result，实际 {len(results)}"
    assert all(not r.is_error for r in results), f"全部应成功: {[r.result for r in results]}"
    assert {r.tool_use_id for r in results} == {"call_a", "call_b", "call_c"}

    # 收尾仍以 summary + complete 结束
    texts = "".join(e.text for e in events if e.type == "text_delta")
    assert "全部处理完成" in texts
    assert events[-1].type == "complete"


def test_out_of_order_results_matched_by_id():
    """乱序完成：先完成的先 yield，最终消息按 tool_use_id 精确匹配。"""
    backend = MultiToolBackend([
        {"id": "call_slow", "name": "query_inventory", "input": {"channel": "taobao"}},
        {"id": "call_fast", "name": "query_order_status", "input": {"channel": "taobao"}},
    ])

    async def slow_handler(**kw):
        await asyncio.sleep(0.05)
        return "慢查询结果"

    events = run_until_complete(backend, {
        "query_inventory": slow_handler,
        "query_order_status": lambda **kw: "快查询结果",
    })

    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 2
    # FIRST_COMPLETED 语义：快任务先 yield
    assert results[0].tool_use_id == "call_fast", f"快任务应先返回，实际 {results[0].tool_use_id}"
    assert results[1].tool_use_id == "call_slow"
    # 结果与调用 ID 严格对应（乱序不串味）
    by_id = {r.tool_use_id: r.result for r in results}
    assert by_id["call_fast"] == "快查询结果"
    assert by_id["call_slow"] == "慢查询结果"
    assert events[-1].type == "complete"


def test_partial_failure_isolated():
    """局部失败：单工具异常不拖垮整轮，其余工具照常成功，turn 正常收尾。"""
    backend = MultiToolBackend([
        {"id": "call_ok1", "name": "query_inventory", "input": {"channel": "taobao"}},
        {"id": "call_bad", "name": "update_price", "input": {"channel": "taobao"}},
        {"id": "call_ok2", "name": "create_promotion", "input": {"channel": "taobao"}},
    ])
    events = run_until_complete(backend, {
        "query_inventory": lambda **kw: "库存 10",
        "create_promotion": lambda **kw: "促销已创建",
        "update_price": lambda **kw: (_ for _ in ()).throw(RuntimeError("平台超时")),
    })

    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 3
    bad = {r.tool_use_id: r for r in results}["call_bad"]
    assert bad.is_error and "平台超时" in bad.result, "失败工具应以 is_error=True 回传异常信息"
    ok = [r for r in results if r.tool_use_id in ("call_ok1", "call_ok2")]
    assert all(not r.is_error for r in ok), "并行工具不应被单个失败拖累"
    assert events[-1].type == "complete", "局部失败后 turn 仍应正常收尾"


def test_missing_params_yield_error():
    """缺失参数：handler 收到不完整 input，TypeError 应转成 is_error=True 而非穿透异常。"""
    backend = MultiToolBackend([
        {"id": "call_missing", "name": "update_price", "input": {"channel": "taobao"}},  # 缺 new_price
    ])
    events = run_until_complete(backend, {
        "update_price": lambda channel, new_price: f"改价 {new_price}",
    })

    results = [e for e in events if e.type == "tool_result"]
    assert len(results) == 1
    assert results[0].is_error, "缺参 TypeError 应被捕获为 is_error=True"
    assert "missing" in results[0].result.lower() or "required" in results[0].result.lower() \
        or "new_price" in results[0].result, f"错误应含缺失参数线索: {results[0].result}"
    assert events[-1].type == "complete"