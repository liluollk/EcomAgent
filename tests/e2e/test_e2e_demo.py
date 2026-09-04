"""E2E 全链路测试 — 对齐 README「已验证」的声明，全部离线可跑。

评测体系分层：
  L3 行为契约场景 — 评测集为独立数据表（scenarios.py），本文件只负责
     按表执行与断言（统一驱动器 test_e2e_behavior_contract_scenarios）。
     新增场景 = 在 scenarios.py 加一行数据，无需改动断言逻辑。
  非对话链路 — 幂等回放（REST 层）与 /sessions 冒烟保留为独立用例。

驱动方式：真实 MCP stdio 子进程（mcp_pool.connect）+ platform=mock 离线 ASGI 兜底
（无需平台子进程/TCP）+ AGENT_BACKEND=mock 剧本后端 + 真实权限管线。隔离
AGENT_STORAGE_DIR / CHANNEL_CONFIG_FILE / MEMORY_DIR，不污染仓库 data/。
"""

import asyncio

import pytest

import sources.channel_registry as _cr
from events.agent_event import (
    ToolResultEvent,
    ToolStartEvent,
)
from session.session import PermissionMode, Session
from session.workspace import Workspace

from scenarios import SCENARIOS


# ---------------------------------------------------------------------------
# 隔离
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def e2e_isolate(tmp_path, monkeypatch):
    """隔离所有运行时目录与注册表缓存，避免污染 data/ 与跨用例状态。"""
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    # 关闭平台子进程自动拉起：离线 ASGI 兜底足够（不依赖 CHANNEL_API_URL）
    monkeypatch.setenv("CHANNEL_API_AUTO", "0")
    monkeypatch.setenv("AGENT_BACKEND", "mock")

    _cr._store._mtime = -1
    _cr._store._channels = None
    _cr.DEFAULT_CHANNEL_REGISTRY._clients.clear()

    # 记忆 store 重置到隔离目录（模块级单例需重新注入）
    from agent_runtime import memory_store as _ms

    fresh = _ms.MemoryStore(root=str(tmp_path / "memory"))
    monkeypatch.setattr("agent_runtime.base_agent.DEFAULT_MEMORY_STORE", fresh)
    monkeypatch.setattr("agent_runtime.memory_store.DEFAULT_MEMORY_STORE", fresh)

    # 后端 provider 注册表指向 mock（离线剧本后端）
    import agent_backend.provider_registry as _pr

    monkeypatch.setenv("PROVIDER_CONFIG_FILE", str(tmp_path / "providers.json"))
    _pr._store._mtime = -1
    _pr._store._providers = None
    _pr._store._active = "openai"

    yield
    _cr.DEFAULT_CHANNEL_REGISTRY._clients.clear()


# ---------------------------------------------------------------------------
# 场景钩子（setup / after 与场景表解耦：钩子名在 scenarios.py，实现在此处）
# ---------------------------------------------------------------------------


def _setup_add_pdd_channel():
    """动态渠道场景前置：注册新渠道 pdd 并确认已生效。"""
    from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

    DEFAULT_CHANNEL_REGISTRY.add({"name": "pdd", "label": "拼多多"})
    assert "pdd" in DEFAULT_CHANNEL_REGISTRY.enabled_names()


def _after_memory_landed():
    """跨会话记忆场景收尾：断言显式记忆已落盘且可检索注入。"""
    from agent_runtime import memory_store as _ms

    store = _ms.DEFAULT_MEMORY_STORE
    assert "3 万件" in store.recall_section("default", query="双11")


_SETUP_HOOKS = {"add_pdd_channel": _setup_add_pdd_channel}
_AFTER_HOOKS = {"assert_memory_landed": _after_memory_landed}


# ---------------------------------------------------------------------------
# 驱动 helper
# ---------------------------------------------------------------------------


def _make_session(workspace: Workspace, sid: str, mode: PermissionMode = PermissionMode.EXECUTE) -> Session:
    return Session(
        session_id=sid,
        workspace=workspace,
        permission_mode=mode,
        active_sources=["taobao", "jd", "douyin"],
        # 六步链路含改价/促销/上下架/工单，用 manager（店长）保证 ACL 全覆盖
        user={"user_id": "e2e_user", "role": "manager"},
    )


def _make_workspace() -> Workspace:
    return Workspace(
        workspace_id="default",
        name="OceanBreeze",
        metadata={"brand": "OceanBreeze"},
        rules=[{"type": "price_above_cost"}],
    )


def _first_tool_start(events: list) -> ToolStartEvent:
    """返回第一个业务工具调用（跳过渐进式加载的 load_skill 元调用）。"""
    for ev in events:
        if isinstance(ev, ToolStartEvent) and ev.tool_name != "load_skill":
            return ev
    raise AssertionError(f"未发现业务 tool_start 事件: {[e.type for e in events]}")


def _tool_results(events: list) -> list[ToolResultEvent]:
    return [e for e in events if isinstance(e, ToolResultEvent) and e.tool_name != "load_skill"]


def _has_load_skill(events: list) -> bool:
    """断言渐进式加载的第一步：load_skill 元调用出现过。"""
    return any(isinstance(e, ToolStartEvent) and e.tool_name == "load_skill" for e in events)


def _run_e2e(scenario):
    """在单次 asyncio.run 中执行完整 MCP 联动场景。

    MCP 的 stdio_client/ClientSession 绑定创建时的事件循环，跨 asyncio.run
    复用会触发 anyio 的 cancel-scope 错误；因此连接 → 建 agent → 全部对话轮
    → 关闭 必须在同一次 run 内完成。
    """

    async def _main():
        from transport.state import mcp_pool, _build_agent, _build_tools

        await mcp_pool.connect()
        try:
            ws = _make_workspace()
            session = _make_session(ws, "e2e")
            agent = _build_agent(session)

            async def chat(text: str) -> list:
                tools = _build_tools()
                return [ev async for ev in agent.chat(session, text, tools)]

            return await scenario(chat)
        finally:
            await mcp_pool.close()

    return asyncio.run(_main())


# ---------------------------------------------------------------------------
# L3 统一驱动器：按场景表执行与断言
# ---------------------------------------------------------------------------

_STEP_MISSING_MSG = "[{name}] step「{msg}」期望工具 {tool}，实际 {actual}"
_STEP_INPUT_MSG = "[{name}] step「{msg}」参数 {k}={actual} 期望 {v}"
_STEP_ERROR_MSG = "[{name}] step「{msg}」期望失败结果，实际成功"
_STEP_OK_MSG = "[{name}] step「{msg}」工具执行失败: {result}"
_STEP_COMPLETE_MSG = "[{name}] step「{msg}」未以 complete 收尾"


def _assert_step(scenario_name: str, step: dict, events: list) -> None:
    """对单步对话事件流做契约断言（期望轨迹匹配）。"""
    assert _has_load_skill(events), f"[{scenario_name}] step「{step['message']}」未出现 load_skill（渐进式加载第一步）"

    if step.get("expect_tool", True):
        ts = _first_tool_start(events)
        assert ts.tool_name == step["tool"], _STEP_MISSING_MSG.format(
            name=scenario_name, msg=step["message"], tool=step["tool"], actual=ts.tool_name
        )
        for k, v in step.get("input", {}).items():
            assert ts.input.get(k) == v, _STEP_INPUT_MSG.format(
                name=scenario_name, msg=step["message"], k=k, actual=ts.input.get(k), v=v
            )

    results = _tool_results(events)
    assert len(results) >= 1, f"[{scenario_name}] step「{step['message']}」未产生 tool_result"
    if step.get("result_is_error"):
        assert results[0].is_error, _STEP_ERROR_MSG.format(name=scenario_name, msg=step["message"])
        for token in step.get("result_contains", []):
            assert token in results[0].result, (
                f"[{scenario_name}] step「{step['message']}」失败文本缺「{token}」: {results[0].result}"
            )
    else:
        assert not results[0].is_error, _STEP_OK_MSG.format(
            name=scenario_name, msg=step["message"], result=results[0].result
        )

    assert any(e.type == "complete" for e in events), _STEP_COMPLETE_MSG.format(
        name=scenario_name, msg=step["message"]
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["name"])
def test_e2e_behavior_contract_scenarios(scenario):
    """按评测集数据表驱动：协议一致的执行 + 断言，覆盖六步链路/成本拦截/动态渠道/跨会话记忆。"""

    async def run(chat):
        hook = _SETUP_HOOKS.get(scenario.get("setup", ""))
        if hook:
            hook()
        for step in scenario["steps"]:
            events = await chat(step["message"])
            _assert_step(scenario["name"], step, events)
        after = _AFTER_HOOKS.get(scenario.get("after", ""))
        if after:
            after()
        return "ok"

    assert _run_e2e(run) == "ok"


# ---------------------------------------------------------------------------
# 幂等回放（REST 层，显式同 key 两次 POST）— 非对话链路，独立保留
# ---------------------------------------------------------------------------


def test_e2e_idempotency_replay():
    from httpx import ASGITransport
    from mocks.channel_api_mock import app as _api_app
    from sources.rest_client import ChannelRestClient

    async def _run():
        client = ChannelRestClient(base_url="http://test", transport=ASGITransport(_api_app))
        try:
            first = await client.call(
                "POST", "/v1/taobao/promotions",
                json_body={"sku": "SKU-1", "discount": 0.8, "start_time": "t1", "end_time": "t2"},
                idempotency_key="same-key-001",
            )
            second = await client.call(
                "POST", "/v1/taobao/promotions",
                json_body={"sku": "SKU-1", "discount": 0.8, "start_time": "t1", "end_time": "t2"},
                idempotency_key="same-key-001",
            )
            return first, second
        finally:
            await client.aclose()

    first, second = asyncio.run(_run())
    assert first.get("idempotent_replay") is not True, "首次创建不应标记 replay"
    assert second.get("idempotent_replay") is True, "同 key 二次请求应标记幂等回放"


# ---------------------------------------------------------------------------
# REST 层 /sessions 与 workspace 冒烟（E2E 前置链路）
# ---------------------------------------------------------------------------


def test_e2e_rest_session_and_workspace_smoke():
    from httpx import ASGITransport, AsyncClient
    from transport.server import app

    async def _run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 建 workspace + 会话
            r = await client.post("/workspaces", json={"workspace_id": "brand_a", "name": "品牌A"})
            assert r.status_code == 200
            r = await client.post("/sessions", json={"workspace_id": "brand_a", "permission_mode": "EXECUTE"})
            assert r.status_code == 200
            sid = r.json()["session_id"]
            assert r.json()["workspace_id"] == "brand_a"
            # 会话列表
            listed = (await client.get("/sessions")).json()
            assert any(s["session_id"] == sid for s in listed)
            # 工作空间列表
            wss = (await client.get("/workspaces")).json()
            assert any(w["workspace_id"] == "brand_a" for w in wss)

    asyncio.run(_run())