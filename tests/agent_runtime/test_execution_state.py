"""Session 执行状态测试 — turn 生命周期内 execution_state 迁移。"""

import asyncio

from agent_backend.protocol import AgentCapabilities, BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import ToolStartEvent
from permission.pre_tool_use import PreToolUseAction, PreToolUsePipeline, PreToolUseResult
from session.session import ExecutionState, PermissionMode, Session
from session.workspace import Workspace


class AskWriteBackend:
    """脚本化后端：第一轮产出 update_price 工具调用。"""

    def __init__(self) -> None:
        self.rounds = 0

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(supports_tool_calling=True, supports_thinking_level=False,
                                 supports_json_mode=True, context_window=128000)

    def get_config(self) -> BackendConfig:
        return BackendConfig(provider=BackendProvider.OPENAI, model="mock", api_key="t")

    def update_runtime_config(self, config: BackendConfig) -> None:
        pass

    def abort(self, reason: str) -> None:
        pass

    async def chat(self, messages: list[dict], tools: list[dict], session_id: str):
        self.rounds += 1
        if self.rounds == 1:
            yield ToolStartEvent(
                tool_name="update_price",
                tool_use_id="call_1",
                input={"channel": "taobao", "sku": "TB-001", "new_price": 100, "cost_price": 50},
            )
        else:
            from events.agent_event import TextDeltaEvent
            yield TextDeltaEvent(text="完成")


def ask_rule(tool_name: str, tool_input: dict) -> PreToolUseResult:
    return PreToolUseResult(action=PreToolUseAction.ASK, reason="需确认")


def make_session() -> Session:
    ws = Workspace(workspace_id="w1", name="test")
    return Session(session_id="s1", workspace=ws, permission_mode=PermissionMode.ASK,
                   active_sources=["taobao"], user={"user_id": "u1", "role": "operator"})


def test_execution_state_transitions():
    """RUNNING →（ASK 挂起）WAITING_PERMISSION → RUNNING → COMPLETED。"""

    async def scenario():
        backend = AskWriteBackend()
        session = make_session()
        agent = BaseAgent(backend, session.workspace)
        pipeline = PreToolUsePipeline()
        pipeline.add_checker(ask_rule)
        agent.set_permission_pipeline(pipeline)
        agent.set_tool_handlers({"update_price": lambda **kw: "ok"})
        states: list[ExecutionState] = []

        async def resolver(request) -> bool:
            states.append(session.execution_state)  # 挂起中观测
            return True

        agent.set_permission_resolver(resolver)
        async for ev in agent.chat(session, "改价", [{"name": "update_price", "description": "", "parameters": {}}]):
            pass
        assert session.execution_state == ExecutionState.COMPLETED
        assert ExecutionState.WAITING_PERMISSION in states

    asyncio.run(scenario())


def test_execution_state_aborted():
    """abort 后 execution_state 置 ABORTED。"""

    async def scenario():
        backend = AskWriteBackend()
        session = make_session()
        agent = BaseAgent(backend, session.workspace)
        pipeline = PreToolUsePipeline()
        pipeline.add_checker(ask_rule)
        agent.set_permission_pipeline(pipeline)
        agent.set_tool_handlers({"update_price": lambda **kw: "ok"})
        agent.abort("test")
        async for _ in agent.chat(session, "改价", [{"name": "update_price", "description": "", "parameters": {}}]):
            pass
        assert session.execution_state == ExecutionState.ABORTED

    asyncio.run(scenario())