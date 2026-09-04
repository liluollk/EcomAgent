"""BaseAgent 中断事件测试 — 流式中途 abort 应以 AbortEvent 收尾而非 CompleteEvent。"""

import asyncio

from agent_backend.protocol import BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import TextDeltaEvent
from session.session import ExecutionState, Session
from session.workspace import Workspace


class StreamingBackend:
    """持续产出文本增量、不调用工具的脚本后端。"""

    def __init__(self) -> None:
        self._aborted = False

    def capabilities(self):
        return None

    def get_config(self) -> BackendConfig:
        return BackendConfig(provider=BackendProvider.OPENAI, model="mock", api_key="test")

    def update_runtime_config(self, config: BackendConfig) -> None:
        pass

    def abort(self, reason: str) -> None:
        self._aborted = True

    async def chat(self, messages: list[dict], tools: list[dict], session_id: str):
        for i in range(50):
            yield TextDeltaEvent(text=f"片段{i} ")


def make_session() -> Session:
    workspace = Workspace(workspace_id="w1", name="test", metadata={"brand": "B"})
    return Session(session_id="s1", workspace=workspace, active_sources=["taobao"])


def collect_with_abort(agent: BaseAgent, session: Session, message: str) -> list:
    """逐事件消费 chat 流，首个文本增量后调用 abort，再消费至流结束。"""

    async def run() -> list:
        collected: list = []
        iterator = agent.chat(session, message, []).__aiter__()
        while True:
            try:
                event = await iterator.__anext__()
            except StopAsyncIteration:
                break
            collected.append(event)
            if event.type == "text_delta" and not any(
                e.type == "abort" for e in collected
            ):
                agent.abort("user_cancel")
        return collected

    return asyncio.run(run())


def collect_all(agent: BaseAgent, session: Session, message: str) -> list:
    async def run() -> list:
        return [event async for event in agent.chat(session, message, [])]

    return asyncio.run(run())


def test_abort_mid_stream_ends_with_abort_event():
    """流式中途中断：事件流以 AbortEvent(reason) 收尾，且不得出现 CompleteEvent。"""
    session = make_session()
    agent = BaseAgent(StreamingBackend(), session.workspace)
    events = collect_with_abort(agent, session, "生成一份长报告")

    assert events, "事件流不应为空"
    assert events[-1].type == "abort"
    assert events[-1].reason == "user_cancel"

    types = [e.type for e in events]
    assert "complete" not in types, "中断的 turn 不应发出 complete 事件"
    assert "abort" in types
    assert session.execution_state == ExecutionState.ABORTED


def test_normal_completion_still_ends_with_complete_event():
    """回归保护：未中断的正常 turn 仍以 CompleteEvent 收尾且无 AbortEvent。"""
    session = make_session()
    agent = BaseAgent(StreamingBackend(), session.workspace)
    events = collect_all(agent, session, "你好")

    types = [e.type for e in events]
    assert types[-1] == "complete"
    assert "abort" not in types
    assert session.execution_state == ExecutionState.COMPLETED
