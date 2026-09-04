"""多品牌 workspace 隔离 API 测试 — /workspaces CRUD、建会话绑定 workspace、持久化恢复。"""

import os
import pytest
from httpx import ASGITransport, AsyncClient
from transport.server import app, sessions, workspaces


@pytest.fixture(autouse=True)
def cleanup(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_STORAGE_DIR", str(tmp_path / "sessions"))
    sessions.clear()
    workspaces.clear()
    yield
    sessions.clear()
    workspaces.clear()


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_workspaces_has_default():
    async with await _client() as client:
        resp = await client.get("/workspaces")
        assert resp.status_code == 200
        body = resp.json()
        assert any(w["workspace_id"] == "default" for w in body)
        default = next(w for w in body if w["workspace_id"] == "default")
        assert default["brand"] == "OceanBreeze"


async def test_create_workspace_and_list():
    async with await _client() as client:
        resp = await client.post(
            "/workspaces",
            json={"workspace_id": "brand_a", "name": "品牌A", "brand": "BrandA"},
        )
        assert resp.status_code == 200
        assert resp.json()["brand"] == "BrandA"
        listed = (await client.get("/workspaces")).json()
        assert any(w["workspace_id"] == "brand_a" for w in listed)


async def test_create_duplicate_400_and_missing_name_400():
    async with await _client() as client:
        await client.post("/workspaces", json={"workspace_id": "brand_a"})
        assert (await client.post("/workspaces", json={"workspace_id": "brand_a"})).status_code == 400
        assert (await client.post("/workspaces", json={})).status_code == 400


async def test_create_session_binds_workspace():
    async with await _client() as client:
        await client.post("/workspaces", json={"workspace_id": "brand_b", "brand": "BrandB"})
        resp = await client.post("/sessions", json={"workspace_id": "brand_b", "role": "operator"})
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == "brand_b"
        # 会话列表应带 workspace_name
        listed = (await client.get("/sessions")).json()
        s = next(x for x in listed if x["session_id"] == resp.json()["session_id"])
        assert s["workspace_id"] == "brand_b"
        assert s["workspace_name"] == "brand_b"


async def test_create_session_default_workspace():
    async with await _client() as client:
        resp = await client.post("/sessions")
        assert resp.json()["workspace_id"] == "default"


async def test_workspace_isolated_rules():
    """不同 workspace 的规则独立：brand_a 无 cost 规则时 update_price 不拦截，
    default 有 price_above_cost 规则时拦截低于成本价的调价。"""
    from permission.pre_tool_use import PreToolUseAction
    from permission.rule_engine import workspace_rules_rule
    from transport.state import _get_workspace_or_create

    # default 规则含 price_above_cost
    ws_default = _get_workspace_or_create("default")
    gate = workspace_rules_rule(ws_default.rules)
    r = gate("update_price", {"new_price": 20, "cost_price": 59})
    assert r.action == PreToolUseAction.BLOCK  # 低于成本价拦截

    # brand_a 通过 API 创建时指定空规则
    async with await _client() as client:
        resp = await client.post(
            "/workspaces",
            json={"workspace_id": "brand_a", "name": "品牌A", "rules": []},
        )
        assert resp.status_code == 200
        ws_a = _get_workspace_or_create("brand_a")
        gate2 = workspace_rules_rule(ws_a.rules)
        r2 = gate2("update_price", {"new_price": 20, "cost_price": 59})
        assert r2.action == PreToolUseAction.ALLOW  # 无规则 → 放行