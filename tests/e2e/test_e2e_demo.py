"""E2E 全链路测试 — 对齐 README「已验证」的声明，全部离线可跑。

评测体系分层：
  L3 行为契约场景 — 评测集为独立数据表（harness/cases.py），本文件只负责
     按表执行与断言（统一驱动器 test_e2e_behavior_contract_scenarios）。
     新增场景 = 在 harness/cases.py 加一行数据，无需改动断言逻辑。
     断言器与钩子复用 harness/assertions.py、harness/hooks.py（与独立评测
     入口 python -m harness 共用单一事实源）。
  非对话链路 — 幂等回放（REST 层）与 /sessions 冒烟保留为独立用例。

驱动方式：真实 MCP stdio 子进程（mcp_pool.connect）+ platform=mock 离线 ASGI 兜底
（无需平台子进程/TCP）+ AGENT_BACKEND=mock 剧本后端 + 真实权限管线。隔离
AGENT_STORAGE_DIR / CHANNEL_CONFIG_FILE / MEMORY_DIR，不污染仓库 data/。
"""

import asyncio

import pytest

import sources.channel_registry as _cr
from session.session import PermissionMode, Session
from session.workspace import Workspace

from harness.assertions import assert_step
from harness.cases import SCENARIOS
from harness.hooks import get_after_hook, get_setup_hook


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
    # 用户技能隔离（save_skill 落盘处；注册表单例需显式重定向目录）
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    import sources.skill_registry as _sr

    _sr.DEFAULT_SKILL_REGISTRY._user_dir = tmp_path / "skills"
    _sr.DEFAULT_SKILL_REGISTRY._cached_mtime = -1

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
# 驱动 helper
# ---------------------------------------------------------------------------


def _make_session(
    workspace: Workspace,
    sid: str,
    mode: PermissionMode = PermissionMode.EXECUTE,
    role: str = "manager",
) -> Session:
    return Session(
        session_id=sid,
        workspace=workspace,
        permission_mode=mode,
        active_sources=["taobao", "jd", "douyin"],
        # 缺省 manager（店长）保证写操作 ACL 全放行；场景可用 mode/role 覆盖
        # （如 readonly_blocks_write / rbac_denial）
        user={"user_id": "e2e_user", "role": role},
    )


def _make_workspace() -> Workspace:
    return Workspace(
        workspace_id="default",
        name="OceanBreeze",
        metadata={"brand": "OceanBreeze"},
        rules=[{"type": "price_above_cost"}],
    )


def _run_e2e(driver, scenario):
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
            session = _make_session(
                ws,
                "e2e",
                mode=getattr(PermissionMode, scenario.get("mode", "EXECUTE")),
                role=scenario.get("role", "manager"),
            )
            agent = _build_agent(session)

            async def chat(text: str) -> list:
                tools = _build_tools()
                return [ev async for ev in agent.chat(session, text, tools)]

            return await driver(chat)
        finally:
            await mcp_pool.close()

    return asyncio.run(_main())


# ---------------------------------------------------------------------------
# L3 统一驱动器：按场景表执行与断言（断言规则在 harness/assertions.py）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["name"])
def test_e2e_behavior_contract_scenarios(scenario):
    """按评测集数据表驱动：协议一致的执行 + 断言，覆盖六步链路/成本拦截/动态渠道/跨会话记忆。"""

    async def run(chat):
        hook = get_setup_hook(scenario.get("setup"))
        if hook:
            hook()
        for step in scenario["steps"]:
            events = await chat(step["message"])
            assert_step(scenario["name"], step, events)
        after = get_after_hook(scenario.get("after"))
        if after:
            after()
        return "ok"

    assert _run_e2e(run, scenario) == "ok"


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