"""Mock Commerce API — 确定性故障脚本与幂等写语义测试（ASGI 真实 HTTP）。"""

import asyncio

import httpx
import pytest

from mock_commerce import fault_injection
from mock_commerce.auth import MOCK_API_KEY
from mock_commerce.domain import reset_idempotency
from mock_commerce.routes import app
from mock_commerce.store import reset_writes, writes_of


@pytest.fixture(autouse=True)
def _reset_fault_state():
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    yield
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"X-Api-Key": MOCK_API_KEY},
    )


def _inventory(client: httpx.AsyncClient, channel: str = "taobao", sku: str = "SKU-001"):
    return client.get(f"/v1/{channel}/inventory", params={"sku": sku})


def test_rate_limit_once_then_success_script():
    """确定性脚本：第一步 429，随后恢复正常（同输入必同轨迹）。"""

    async def _run():
        fault_injection.load_script("rate_limit_once_then_success")
        async with _client() as c:
            r1 = await _inventory(c)
            assert r1.status_code == 429
            assert r1.json()["detail"]["code"] == 10005
            r2 = await _inventory(c)
            assert r2.status_code == 200
            assert r2.json()["data"]["stock"] == 1523
            r3 = await _inventory(c)
            assert r3.status_code == 200, "脚本耗尽后恒为正常响应"

    asyncio.run(_run())


def test_malformed_once_script_returns_envelope_ok_but_bad_data():
    """畸形响应：HTTP 200 + code=0 信封完好，但 data 含类型违例字段（由校验层拦截）。"""

    async def _run():
        fault_injection.load_script("malformed_once_then_success")
        async with _client() as c:
            r1 = await _inventory(c)
            assert r1.status_code == 200
            body = r1.json()
            assert body["code"] == 0
            assert body["data"]["stock"] == "N/A"  # int 位被字符串污染
            r2 = await _inventory(c)
            assert r2.json()["data"]["stock"] == 1523

    asyncio.run(_run())


def test_write_side_effect_applied_before_timeout_sleep():
    """写操作「先落副作用、后超时挂起」：副作用已记录（复现真实危险场景）。"""

    async def _run():
        fault_injection.load_script("timeout_once_then_success", timeout_seconds=0.05)
        async with _client() as c:
            resp = await c.put(
                "/v1/taobao/price",
                json={"sku": "SKU-001", "new_price": 89.0},
                headers={"X-Idempotency-Key": "k-timeout-1"},
            )
            assert resp.status_code == 200
            assert len(writes_of("update_price")) == 1

    asyncio.run(_run())


def test_idempotent_put_price_replay_no_double_write():
    """同幂等键重复 PUT：回放首次结果，写副作用只落一次。"""

    async def _run():
        async with _client() as c:
            r1 = await c.put(
                "/v1/taobao/price",
                json={"sku": "SKU-001", "new_price": 89.0},
                headers={"X-Idempotency-Key": "k-1"},
            )
            r2 = await c.put(
                "/v1/taobao/price",
                json={"sku": "SKU-001", "new_price": 999.0},
                headers={"X-Idempotency-Key": "k-1"},
            )
            assert r1.json()["data"].get("idempotent_replay") is not True
            assert r2.json()["data"]["idempotent_replay"] is True
            assert r2.json()["data"]["new_price"] == 89.0, "回放应返回首次结果"
            assert len(writes_of("update_price")) == 1
            r3 = await c.put("/v1/taobao/price", json={"sku": "SKU-002", "new_price": 66.0})
            assert r3.json()["data"].get("idempotent_replay") is not True
            assert len(writes_of("update_price")) == 2

    asyncio.run(_run())


def test_permanent_500_and_unknown_script_name():
    async def _run():
        fault_injection.load_script("permanent_500")
        async with _client() as c:
            r = await _inventory(c)
            assert r.status_code == 500
            assert r.json()["detail"]["code"] == 99999
        with pytest.raises(ValueError):
            fault_injection.load_script("no_such_script")

    asyncio.run(_run())


def test_env_static_fault_rate_limited(monkeypatch):
    """环境变量静态故障：独立进程演练形态全程生效。"""

    async def _run():
        monkeypatch.setenv("FAULT_SCENARIO", "rate_limited")
        async with _client() as c:
            r = await _inventory(c)
            assert r.status_code == 429

    asyncio.run(_run())
