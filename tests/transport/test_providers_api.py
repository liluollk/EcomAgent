"""模型供应商管理 API 测试 — 设置里切换供应商/切模型/填密钥/激活（配置化骨架）。"""

import pytest
from httpx import ASGITransport, AsyncClient

import agent_backend.provider_registry as _pr
from transport.server import app
from agent_backend.provider_registry import DEFAULT_PROVIDER_REGISTRY


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """隔离供应商配置文件，避免污染 data/providers.json 与跨用例缓存。"""
    monkeypatch.setenv("PROVIDER_CONFIG_FILE", str(tmp_path / "providers.json"))
    _pr._store._mtime = -1
    _pr._store._providers = None
    _pr._store._active = "openai"
    yield
    _pr._store._providers = None
    _pr._store._active = "openai"


async def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_providers_has_three_builtin():
    async with await _client() as client:
        resp = await client.get("/providers")
        assert resp.status_code == 200
        body = resp.json()
        names = {p["name"] for p in body["providers"]}
        assert names == {"openai", "anthropic", "mock"}
        assert body["active"] == "openai"


async def test_add_and_list_provider():
    async with await _client() as client:
        resp = await client.post(
            "/providers",
            json={"name": "zhipu", "label": "智谱", "provider": "openai", "api_key": "sk-x"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "zhipu"
        listed = (await client.get("/providers")).json()
        assert any(p["name"] == "zhipu" for p in listed["providers"])


async def test_add_duplicate_and_builtin_rejected():
    async with await _client() as client:
        assert (await client.post("/providers", json={"name": "openai", "provider": "openai"})).status_code == 400
        assert (await client.post("/providers", json={"name": "slim", "provider": "slim"})).status_code == 400


async def test_update_provider_and_mask_key():
    async with await _client() as client:
        await client.post("/providers", json={"name": "zhipu", "provider": "openai", "api_key": "sk-abcdef"})
        resp = await client.patch("/providers/zhipu", json={"api_key": "sk-zzzz", "default_model": "glm-4"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["default_model"] == "glm-4"
        # 敏感字段掩码返回，不落明文
        assert body["api_key"].endswith("****")


async def test_activate_provider_then_build_config():
    async with await _client() as client:
        await client.post("/providers", json={"name": "zhipu", "provider": "anthropic", "default_model": "glm-x"})
        resp = await client.post("/providers/zhipu/activate")
        assert resp.status_code == 200
        assert resp.json()["active"] == "zhipu"
        cfg = DEFAULT_PROVIDER_REGISTRY.build_config("zhipu")
        assert cfg.model == "glm-x"


async def test_delete_custom_ok_builtin_400_missing_404():
    async with await _client() as client:
        await client.post("/providers", json={"name": "zhipu", "provider": "openai"})
        assert (await client.delete("/providers/zhipu")).status_code == 200
        assert (await client.delete("/providers/openai")).status_code == 400
        assert (await client.delete("/providers/missing")).status_code == 404


async def test_test_provider():
    async with await _client() as client:
        # mock 离线可用
        resp = await client.post("/providers/mock/test")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # openai 无密钥 → 提示缺密钥（离线，不发网络请求）
        resp = await client.post("/providers/openai/test")
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert "API Key" in resp.json()["message"]


def test_effective_provider_precedence(tmp_path, monkeypatch):
    """AGENT_BACKEND env 在无持久化配置时生效；设置页保存后设置优先于 env。"""
    monkeypatch.setenv("AGENT_BACKEND", "mock")
    # 无 providers.json：env 生效 → mock 后端
    monkeypatch.delenv("PROVIDER_CONFIG_FILE", raising=False)
    monkeypatch.setenv("PROVIDER_CONFIG_FILE", str(tmp_path / "providers.json"))
    _pr._store._mtime = -1
    _pr._store._providers = None
    _pr._store._active = "openai"
    cfg = DEFAULT_PROVIDER_REGISTRY.build_config()
    assert cfg.provider.name == "MOCK"

    # 保存过配置且显式激活 anthropic：设置优先于 env
    monkeypatch.setenv("AGENT_BACKEND", "mock")
    DEFAULT_PROVIDER_REGISTRY.set_active("anthropic")
    _pr._store._mtime = -1
    _pr._store._providers = None
    _pr._store._active = "openai"
    cfg = DEFAULT_PROVIDER_REGISTRY.build_config()
    assert cfg.provider.name == "ANTHROPIC"

async def test_build_config_passes_through_thinking_level():
    """供应商配置里的 thinking_level 应透传到 BackendConfig，非法值归 None。"""
    async with await _client() as client:
        await client.post(
            "/providers",
            json={"name": "zhipu", "provider": "openai", "default_model": "o3-mini",
                  "thinking_level": "high"},
        )
        cfg = DEFAULT_PROVIDER_REGISTRY.build_config("zhipu")
        assert cfg.thinking_level == "high"

        # 非法值归一为 None（关闭）
        resp = await client.patch("/providers/zhipu", json={"thinking_level": "ultra"})
        assert resp.status_code == 200
        cfg = DEFAULT_PROVIDER_REGISTRY.build_config("zhipu")
        assert cfg.thinking_level is None
