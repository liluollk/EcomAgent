"""BaseAgent 技能加载测试 — 新语义：技能=纯知识，工具常可见，SOP 按需注入。

- 全部业务工具从第一轮起就可见（load_skill 元工具 + 工具池）；
- load_skill 命中后，技能 SOP 正文注入系统提示词（渐进式披露作用于知识面）；
- 不存在的技能 / 前置条件不满足 → load_skill 返回错误结果。
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
        self.last_messages = list(messages)  # 快照：回合结束后引擎会 pop 注入的 system
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
        self.last_messages = list(messages)  # 快照：回合结束后引擎会 pop 注入的 system
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
            if "已加载" not in last_msg:
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


def test_all_tools_visible_from_first_round():
    """新语义：技能不绑定工具，全部业务工具第一轮起就可见（load_skill + 工具池）。"""
    backend = ProgressiveBackend("inventory_query", "query_inventory", {"channel": "taobao", "sku": "SKU-001"})
    session = make_session()
    collect(backend, session, "查询一下库存")
    names = [t["name"] for t in backend.received_tools_by_round[0]]
    assert names == ["load_skill", "query_inventory", "update_price", "create_promotion", "query_order_status"]


def test_load_skill_injects_sop_into_system_prompt():
    """load_skill 命中后，技能 SOP 正文注入后续轮次的系统提示词。"""
    backend = ProgressiveBackend("inventory_query", "query_inventory", {"channel": "taobao", "sku": "SKU-001"})
    session = make_session()
    events = collect(backend, session, "查询一下库存")

    assert len(backend.received_tools_by_round) >= 2
    system_prompt = backend.last_messages[0]["content"]
    assert "已加载技能 inventory_query" in system_prompt
    assert "执行库存查询 SOP" in system_prompt  # SKILL.md 正文注入

    tool_names = [e.tool_name for e in events if e.type == "tool_start"]
    assert "load_skill" in tool_names and "query_inventory" in tool_names


def test_no_skill_loaded_still_sees_all_tools():
    """未加载任何技能时，业务工具同样常可见（知识注入与工具面解耦）。"""
    backend = RecordingBackend()
    session = make_session()
    collect(backend, session, "你好，今天天气怎么样")
    names = [t["name"] for t in backend.received_tools_by_round[0]]
    assert "load_skill" in names and "query_inventory" in names


def test_load_skill_unknown_skill_returns_error():
    """load_skill 传入不存在的技能名时返回错误结果，不产生 domain 调用。"""
    backend = ProgressiveBackend("no_such_skill", "query_inventory", {})
    session = make_session()
    collect(backend, session, "查询一下库存")

    tool_msgs = [m for m in session.messages if m["role"] == "tool"]
    assert tool_msgs and "不存在" in tool_msgs[0]["content"]
    assert "query_inventory" not in [c["tool_name"] for c in session.tool_calls]


def test_unmet_prerequisite_blocks_skill_load():
    """技能带 source:<name> 前置条件且渠道未激活时，load_skill 被拒。"""
    from sources.skill_registry import Skill, SkillRegistry

    registry = SkillRegistry(load_persisted=False)
    registry.register(Skill(
        name="douyin_live",
        description="抖音直播带货",
        keywords=["直播"],
        prerequisites=["source:douyin"],
        body="执行直播 SOP。",
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