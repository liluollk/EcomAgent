"""BaseAgent 身份门测试 — RBAC 角色在 PreToolUse 管线中的拦截与放行、审计带 user。"""

from agent_backend.protocol import AgentCapabilities, BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import TextDeltaEvent, ToolStartEvent
from permission.rbac import CUSTOMER_SERVICE, OPERATOR, FINANCE, role_gate_rule
from permission.pre_tool_use import PreToolUsePipeline
from session.session import PermissionMode, Session
from session.workspace import Workspace


class WriteBackend:
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
                input={"channel": "taobao", "sku": "TB-001", "new_price": 88, "cost_price": 50},
            )
            yield TextDeltaEvent(text="我来操作")
        else:
            yield TextDeltaEvent(text="完成")


def make_session(role: str) -> Session:
    ws = Workspace(workspace_id="w1", name="test")
    return Session(
        session_id="s1",
        workspace=ws,
        permission_mode=PermissionMode.ASK,
        active_sources=["taobao"],
        user={"user_id": "u1", "role": role},
    )


async def run_turn(role: str) -> Session:
    backend = WriteBackend()
    session = make_session(role)
    agent = BaseAgent(backend, session.workspace)
    pipeline = PreToolUsePipeline()
    pipeline.add_checker(role_gate_rule(role))
    agent.set_permission_pipeline(pipeline)
    agent.set_tool_handlers({"update_price": lambda **kw: "ok"})

    events = []
    async for ev in agent.chat(session, "改价", [{"name": "update_price", "description": "", "parameters": {}}]):
        events.append(ev)
    return session, events


def test_cs_role_gate_blocks_write():
    """客服会话执行 update_price：身份门 BLOCK，合成 is_error 结果回传。"""
    import asyncio
    session, events = asyncio.run(run_turn(CUSTOMER_SERVICE))
    from events.agent_event import ToolResultEvent
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results and results[0].is_error is True
    assert "无权" in results[0].result
    # 审计记录带 user
    assert session.tool_calls[0]["user"]["role"] == CUSTOMER_SERVICE


def test_operator_role_gate_allows_write():
    """运营会话执行 update_price：放行执行，handler 返回正常结果。"""
    import asyncio
    session, events = asyncio.run(run_turn(OPERATOR))
    from events.agent_event import ToolResultEvent
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results and results[0].is_error is False
    assert results[0].result == "ok"
    assert session.tool_calls[0]["user"]["role"] == OPERATOR


def test_finance_role_gate_blocks_write():
    """财务会话执行 update_price：身份门 BLOCK（只读角色）。"""
    import asyncio
    session, events = asyncio.run(run_turn(FINANCE))
    from events.agent_event import ToolResultEvent
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results and results[0].is_error is True
    assert "财务" in results[0].result