"""BaseAgent 渐进式技能加载测试 — 先菜单后点菜：load_skill → 工具按需暴露。

新语义（Progressive Skill Loading）：
- 初始轮仅暴露 load_skill 元工具（技能菜单在描述里）；
- 模型调用 load_skill 后，该技能的工具才加入后续轮次的可见工具集；
- 未加载技能的工具对模型不可见。
"""

import asyncio

from agent_backend.protocol import BackendConfig, BackendProvider
from agent_runtime.base_agent import BaseAgent
from events.agent_event import TextDeltaEvent, ToolStartEvent
from session.session import Session
from session.workspace import Workspace

# 模拟渠道池提供的全量工具定义
FULL_TOOLS = [
    {"name": "query_inventory", "description": "查询库存", "parameters": {}},
    {"name": "update_price", "description": "更新价格", "parameters": {}},
    {"name": "create_promotion", "description": "创建促销", "parameters": {}},
    {"name": "query_order_status", "description": "查询订单", "parameters": {}},
]


class RecordingBackend:
    """记录每轮收到的工具列表（received_tools_by_round）与最后一轮消息。"""

    def __init__(self) -> None:
        self.rounds = 0
        self.received_tools_by_round: list[list[dict]] = []
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
        self.received_tools_by_round.append(list(tools))
        self.last_messages = messages
        yield TextDeltaEvent(text="好的")


class ProgressiveBackend(RecordingBackend):
    """第一轮调 load_skill（skill_name），第二轮起调 domain 工具。"""

    def __init__(self, skill_name: str, domain_tool: str, domain_input: dict) -> None:
        super().__init__()
        self._skill_name = skill_name
        self._domain_tool = domain_tool
        self._domain_input = domain_input

    async def chat(self, messages: list[dict], tools: list[dict], session_id: str):
        self.rounds += 1
        self.received_tools_by_round.append(list(tools))
        self.last_messages = messages
        if self.rounds == 1:
            yield ToolStartEvent(
                tool_name="load_skill",
                tool_use_id="call_load",
                input={"skill_name": self._skill_name},
            )
        elif self.rounds == 2:
            # 若 load_skill 失败（技能不存在），第二轮不再调 domain 工具
            last_msg = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "tool"),
                "",
            )
            if "[已加载]" not in last_msg and "已加载" not in last_msg:
                return
            yield ToolStartEvent(
                tool_name=self._domain_tool,
                tool_use_id="call_domain",
                input=self._domain_input,
            )


def make_session(active_sources: list[str] | None = None) -> Session:
    workspace = Workspace(workspace_id="w1", name="test", metadata={"brand": "B"})
    return Session(
        session_id="s1",
        workspace=workspace,
        active_sources=["taobao"] if active_sources is None else active_sources,
    )


def collect(backend, session: Session, message: str, tools: list[dict] | None = None) -> list:
    agent = BaseAgent(backend, session.workspace)
    agent.set_tool_handlers({
        "query_inventory": lambda **kwargs: "库存 320",
        "update_price": lambda **kwargs: "已更新",
    })

    async def run() -> list:
        return [event async for event in agent.chat(session, message, tools or FULL_TOOLS)]

    return asyncio.run(run())


def test_initial_round_only_load_skill_visible():
    """初始轮：模型只看到 load_skill 元工具，业务工具不可见。"""
    backend = ProgressiveBackend("inventory_query", "query_inventory", {"channel": "taobao", "sku": "SKU-001"})
    session = make_session()
    collect(backend, session, "查询一下库存")
    first_round = backend.received_tools_by_round[0]
    names = [t["name"] for t in first_round]
    assert names == ["load_skill"], f"初始轮应只有 load_skill，实际 {names}"


def test_after_load_skill_domain_tools_exposed():
    """load_skill 后第二轮：该技能绑定的工具进入可见工具集。"""
    backend = ProgressiveBackend("inventory_query", "query_inventory", {"channel": "taobao", "sku": "SKU-001"})
    session = make_session()
    events = collect(backend, session, "查询一下库存")

    # 第一轮 tools：只有 load_skill
    assert [t["name"] for t in backend.received_tools_by_round[0]] == ["load_skill"]
    # 第二轮 tools：load_skill + 技能工具
    assert len(backend.received_tools_by_round) >= 2
    names = [t["name"] for t in backend.received_tools_by_round[1]]
    assert "load_skill" in names
    assert "query_inventory" in names, f"加载后应暴露 query_inventory，实际 {names}"
    assert "update_price" not in names, "未加载的技能工具不应暴露"

    # 事件流包含 load_skill 与 domain 工具调用
    tool_names = [e.tool_name for e in events if e.type == "tool_start"]
    assert "load_skill" in tool_names and "query_inventory" in tool_names


def test_general_query_falls_back_to_readonly_tools():
    """未加载任何技能时，模型仍只看到 load_skill（专用工具保持隐藏）。"""
    backend = RecordingBackend()
    session = make_session()
    collect(backend, session, "你好，今天天气怎么样")
    names = [t["name"] for t in backend.received_tools_by_round[0]]
    assert names == ["load_skill"]


def test_load_skill_unknown_skill_returns_error():
    """load_skill 传入不存在的技能名时返回错误结果，工具不扩展。"""
    from events.agent_event import ToolResultEvent

    backend = ProgressiveBackend("no_such_skill", "query_inventory", {})
    session = make_session()
    collect(backend, session, "查询一下库存")

    assert "不存在" in backend.last_messages[-1]["content"]
    # 只有一轮（load_skill 失败后不再调用 domain）
    assert backend.rounds == 1


def test_unmet_prerequisite_blocks_skill_load():
    """技能带 source:<name> 前置条件且渠道未激活时，load_skill 被拒。"""
    from sources.skill_registry import Skill, SkillRegistry

    registry = SkillRegistry(load_persisted=False)
    registry.register(Skill(
        name="douyin_live",
        description="抖音直播带货",
        keywords=["直播"],
        prerequisites=["source:douyin"],
        tools=["query_inventory"],
    ))
    backend = ProgressiveBackend("douyin_live", "query_inventory", {"channel": "douyin"})
    session = make_session(active_sources=["jd"])  # 无 douyin
    agent = BaseAgent(backend, session.workspace, skill_registry=registry)
    agent.set_tool_handlers({"query_inventory": lambda **kwargs: "ok"})

    async def run() -> list:
        return [event async for event in agent.chat(session, "开直播卖货", FULL_TOOLS)]

    events = asyncio.run(run())
    results = [e for e in events if e.type == "tool_result" and e.tool_name == "load_skill"]
    assert results and results[0].is_error, "前置条件不满足应拒绝加载"
    assert "需要激活渠道 douyin" in results[0].result


def test_load_skill_unknown_skill_returns_error():
    """load_skill 传入不存在的技能名时返回错误结果，工具不扩展。"""
    backend = ProgressiveBackend("no_such_skill", "query_inventory", {})
    session = make_session()
    collect(backend, session, "查询一下库存")

    # load_skill 结果带错误信息（via tool_result 事件在 session 消息中体现）
    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    assert tool_msgs and "不存在" in tool_msgs[0]["content"]
    # 错误后不产生 domain 工具调用（query_inventory 不进入 tool_calls）
    assert "query_inventory" not in [c["tool_name"] for c in session.tool_calls]