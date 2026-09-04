"""BaseAgent 权限确认流测试 — ASK 阻塞等待 / 批准执行 / 拒绝回传 / 消息组装。"""

import asyncio

from agent_backend.protocol import AgentCapabilities, BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import TextDeltaEvent, ToolStartEvent
from permission.pre_tool_use import PreToolUseAction, PreToolUsePipeline, PreToolUseResult
from session.session import PermissionMode, Session
from session.workspace import Workspace


class ScriptedBackend:
    """脚本化测试后端：第一轮产出 tool_start，第二轮产出总结文本。"""

    def __init__(self) -> None:
        self.rounds = 0
        self.last_messages: list[dict] = []

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(supports_tool_calling=True, supports_thinking_level=False,
                                 supports_json_mode=True, context_window=128000)

    def get_config(self) -> BackendConfig:
        return BackendConfig(provider=BackendProvider.OPENAI, model="mock", api_key="test")

    def update_runtime_config(self, config: BackendConfig) -> None:
        pass

    def abort(self, reason: str) -> None:
        pass

    async def chat(self, messages: list[dict], tools: list[dict], session_id: str):
        self.rounds += 1
        self.last_messages = messages
        if self.rounds == 1:
            yield TextDeltaEvent(text="我来执行操作")
            yield ToolStartEvent(
                tool_name="update_price",
                tool_use_id="call_1",
                input={"channel": "taobao", "sku": "TB-001", "new_price": 100, "cost_price": 50},
            )
        else:
            yield TextDeltaEvent(text="操作完成")


def ask_all_rule(tool_name: str, tool_input: dict) -> PreToolUseResult:
    """所有工具都要求确认的测试规则。"""
    return PreToolUseResult(action=PreToolUseAction.ASK, reason="测试确认")


def make_session() -> Session:
    workspace = Workspace(workspace_id="w1", name="test", metadata={"brand": "B"})
    return Session(
        session_id="s1",
        workspace=workspace,
        permission_mode=PermissionMode.ASK,
        active_sources=["taobao"],
    )


def make_agent(resolutions: list[bool]):
    """构建挂载 ASK 管线与脚本后端的 Agent，以及按序回放决定的解析器。"""
    backend = ScriptedBackend()
    session = make_session()
    agent = BaseAgent(backend, session.workspace)

    pipeline = PreToolUsePipeline()
    pipeline.add_checker(ask_all_rule)
    agent.set_permission_pipeline(pipeline)
    agent.set_tool_handlers({"update_price": lambda **kwargs: f"ok {kwargs}"})

    async def resolver(request) -> bool:
        return resolutions.pop(0)

    if resolutions is not None:
        agent.set_permission_resolver(resolver)
    return agent, session, backend


def collect(agent: BaseAgent, session: Session) -> list:
    async def run() -> list:
        return [event async for event in agent.chat(session, "把价格调整为 100", [])]

    return asyncio.run(run())


def test_ask_denied_sends_error_result_to_llm():
    agent, session, backend = make_agent([False])
    events = collect(agent, session)

    types = [e.type for e in events]
    assert "permission_request" in types

    denied = [e for e in events if e.type == "tool_result" and e.is_error]
    assert any("[已拒绝]" in e.result for e in denied)

    # 拒绝后不得执行第二轮，也不产生完成文本
    texts = [e.text for e in events if e.type == "text_delta"]
    assert "操作完成" not in "".join(texts)

    # 工具结果必须以合法消息结构回传：assistant(tool_calls) + tool(result)
    assert backend.last_messages[-1]["role"] == "tool"
    assert "[已拒绝]" in backend.last_messages[-1]["content"]
    assistant = [m for m in backend.last_messages if m.get("tool_calls")]
    assert assistant and assistant[0]["tool_calls"][0]["id"] == "call_1"

    # 会话侧权限请求记录已写入决定
    assert session.permission_requests[0]["approved"] is False


def test_ask_approved_executes_and_continues():
    agent, session, backend = make_agent([True])
    events = collect(agent, session)

    results = [e for e in events if e.type == "tool_result"]
    assert any(not r.is_error for r in results), "批准后工具应成功执行"

    texts = "".join(e.text for e in events if e.type == "text_delta")
    assert "操作完成" in texts, "批准后应进入第二轮产出总结"

    assert any(e.type == "complete" for e in events)
    assert session.permission_requests[0]["approved"] is True


def test_missing_resolver_defaults_to_deny():
    """未设置权限解析器时，ASK 请求按安全默认拒绝执行。"""
    agent, session, backend = make_agent(None)
    agent.set_permission_resolver(None)
    events = collect(agent, session)

    denied = [e for e in events if e.type == "tool_result" and e.is_error]
    assert any("[已拒绝]" in e.result for e in denied)
    assert session.permission_requests[0]["approved"] is False
