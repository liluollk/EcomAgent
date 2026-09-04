"""BaseAgent 会话历史结构化测试 — 工具轮消息入库 / 纯文本回复入库 / 历史结构保留。"""

import asyncio

from agent_backend.protocol import BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import TextDeltaEvent, ToolStartEvent
from session.session import Session
from session.workspace import Workspace


class ScriptedBackend:
    """第一轮产出 tool_start，第二轮产出总结文本。"""

    def __init__(self) -> None:
        self.rounds = 0
        self.last_messages: list[dict] = []

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
        self.last_messages = messages
        if self.rounds == 1:
            yield ToolStartEvent(
                tool_name="update_price",
                tool_use_id="call_1",
                input={"channel": "taobao", "sku": "TB-001", "new_price": 100},
            )
        else:
            yield TextDeltaEvent(text="操作完成")


class TextOnlyBackend(ScriptedBackend):
    """只产出文本、不调用工具的后端。"""

    async def chat(self, messages: list[dict], tools: list[dict], session_id: str):
        self.rounds += 1
        self.last_messages = messages
        yield TextDeltaEvent(text="直接回答")


def make_session() -> Session:
    workspace = Workspace(workspace_id="w1", name="test", metadata={"brand": "B"})
    return Session(session_id="s1", workspace=workspace, active_sources=["taobao"])


def run(agent: BaseAgent, session: Session, message: str) -> list:
    async def collect() -> list:
        return [event async for event in agent.chat(session, message, [])]

    return asyncio.run(collect())


def test_tool_round_persists_structured_messages():
    """工具轮的 assistant(tool_calls)、role=tool 消息与审计记录应完整入库。"""
    session = make_session()
    agent = BaseAgent(ScriptedBackend(), session.workspace)
    agent.set_tool_handlers({"update_price": lambda **kwargs: "价格已更新"})
    run(agent, session, "把价格调整为 100")

    msgs = session.messages
    assert msgs[0]["role"] == "user"

    assistant_tc = [m for m in msgs if m.get("tool_calls")]
    assert len(assistant_tc) == 1
    tc = assistant_tc[0]["tool_calls"][0]
    assert tc["id"] == "call_1"
    assert tc["function"]["name"] == "update_price"

    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "价格已更新" in tool_msgs[0]["content"]

    # 第二轮总结文本作为普通 assistant 消息入库
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "操作完成"

    # 工具调用审计记录
    assert len(session.tool_calls) == 1
    record = session.tool_calls[0]
    assert record["tool_name"] == "update_price"
    assert record["input"]["sku"] == "TB-001"
    assert "价格已更新" in record["result"]
    assert record["is_error"] is False


def test_text_only_reply_persisted():
    """无工具调用的纯文本回复也必须写入会话历史。"""
    session = make_session()
    agent = BaseAgent(TextOnlyBackend(), session.workspace)
    run(agent, session, "你好")

    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant"]
    assert session.messages[-1]["content"] == "直接回答"


def test_rebuilt_history_keeps_tool_structure():
    """恢复的历史消息（含 tool_calls）传给后端时结构必须保留。"""
    session = make_session()
    session.add_message("user", "之前的价格调整")
    session.add_message_record({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_old",
            "type": "function",
            "function": {"name": "update_price", "arguments": "{\"sku\": \"TB-001\"}"},
        }],
        "timestamp": "2026-01-01T00:00:00+00:00",
    })
    session.add_message_record({
        "role": "tool",
        "tool_call_id": "call_old",
        "content": "旧结果",
        "timestamp": "2026-01-01T00:00:01+00:00",
    })
    session.mark_saved()

    backend = ScriptedBackend()
    agent = BaseAgent(backend, session.workspace)
    agent.set_tool_handlers({"update_price": lambda **kwargs: "价格已更新"})
    run(agent, session, "继续")

    roles = [m["role"] for m in backend.last_messages]
    assert roles[0] == "user"
    assert "tool_calls" in backend.last_messages[1], "恢复的 assistant 消息必须保留 tool_calls"
    assert backend.last_messages[1]["tool_calls"][0]["id"] == "call_old"
    assert backend.last_messages[2]["tool_call_id"] == "call_old"
