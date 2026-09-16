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


# ---------------------------------------------------------------------------
# 双平台调价确定性脚本（Task 5）：新协议形态下复用同一套故障语义
# ---------------------------------------------------------------------------


def _short_client(timeout: float = 0.3) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"X-Api-Key": MOCK_API_KEY},
        timeout=timeout,
    )


def _taobao_update(price: str = "89.00", sku: str = "SKU-002") -> dict:
    return {
        "method": "taobao.item.sku.price.update",
        "app_key": "mock_app_key", "session": "mock_session", "timestamp": "1700000000",
        "format": "json", "v": "2.0", "sign_method": "hmac-sha256", "sign": "X",
        "shop_id": "SHOP-01", "num_iid": "ITEM-1001", "sku_id": sku, "price": price,
    }


def _douyin_update(price: int = 8900, sku: str = "SKU-002") -> dict:
    return {
        "access_token": "mock_access_token", "app_key": "mock_app_key", "sign": "X",
        "timestamp": 1700000000, "shop_id": "SHOP-01", "product_id": "ITEM-1001",
        "sku_id": sku, "price": price,
    }


def test_write_timeout_before_commit_no_side_effect():
    """写入前超时：副作用落库前客户端已放弃，不得产生写副作用。

    ASGI transport 不会替我们触发 httpx 读超时，这里用 asyncio.wait_for 在测试侧
    模拟「客户端超时放弃」，断言服务端未落副作用。
    """

    async def _run():
        fault_injection.load_script("write_timeout_before_commit", timeout_seconds=1.0)
        async with _short_client() as c:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    c.post("/taobao/top/api", json=_taobao_update(), headers={"X-Idempotency-Key": "k-b"}),
                    timeout=0.3,
                )
            assert len(writes_of("taobao_price_update")) == 0
            # 抖店同样语义
            fault_injection.load_script("write_timeout_before_commit", timeout_seconds=1.0)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    c.post("/douyin/product/sku/price", json=_douyin_update(), headers={"X-Idempotency-Key": "k-b2"}),
                    timeout=0.3,
                )
            assert len(writes_of("douyin_price_update")) == 0

    asyncio.run(_run())


def test_write_timeout_after_commit_exactly_one_side_effect():
    """写入后超时：副作用已落库，客户端超时；重试命中同一幂等键不得新增第二条。"""

    async def _run():
        fault_injection.load_script("write_timeout_after_commit", timeout_seconds=1.0)
        async with _short_client() as c:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    c.post("/taobao/top/api", json=_taobao_update(), headers={"X-Idempotency-Key": "k-a"}),
                    timeout=0.3,
                )
            assert len(writes_of("taobao_price_update")) == 1
            # 同幂等键重试：回放首次结果，副作用仍恰好 1 条
            r = await c.post("/taobao/top/api", json=_taobao_update(), headers={"X-Idempotency-Key": "k-a"})
            assert r.json()["item_sku_price_update_response"]["data"].get("idempotent_replay") is True
            assert len(writes_of("taobao_price_update")) == 1

    asyncio.run(_run())


def test_business_error_permanent_on_new_platform():
    """持续业务错误脚本在新协议形态下返回平台业务码（BUSINESS_ERROR 语义）。"""

    async def _run():
        fault_injection.load_script("business_error_permanent")
        async with _client() as c:
            r = await c.post("/taobao/top/api", json=_taobao_update())
            assert r.json()["error_response"]["code"] == 40
            r = await c.post("/douyin/product/sku/price", json=_douyin_update())
            assert r.json()["code"] == 30001

    asyncio.run(_run())


def test_capability_refused_on_new_platform():
    """能力不支持脚本在新协议形态下返回平台特定能力码。"""

    async def _run():
        fault_injection.load_script("capability_refused")
        async with _client() as c:
            r = await c.post("/taobao/top/api", json=_taobao_update())
            assert r.json()["error_response"]["code"] == 9998
            # 每个平台请求独立消费一步脚本，分开加载以保证两侧都命中
            fault_injection.load_script("capability_refused")
            r = await c.post("/douyin/product/sku/price", json=_douyin_update())
            assert r.json()["code"] == 90000

    asyncio.run(_run())


def test_platform_rate_limit_once_then_success():
    """新平台等价限流脚本：第一步瞬态错误，随后恢复。"""

    async def _run():
        fault_injection.load_script("platform_rate_limit_once_then_success")
        async with _client() as c:
            r = await c.post("/taobao/top/api", json=_taobao_update())
            assert r.json()["error_response"]["code"] == 7
            r = await c.post("/taobao/top/api", json=_taobao_update())
            assert r.json()["item_sku_price_update_response"]["data"]["price"] == "89.00"

    asyncio.run(_run())


def test_platform_malformed_once_then_success():
    """新平台等价畸形脚本：第一步信封完好但 data 畸形，第二步正常。"""

    async def _run():
        fault_injection.load_script("platform_malformed_once_then_success")
        async with _client() as c:
            # 首步返回畸形 data（price 为 "N/A"），请求本身是 update 方法
            r = await c.post("/taobao/top/api", json=_taobao_update())
            assert r.status_code == 200
            assert r.json()["item_sku_price_update_response"]["data"]["price"] == "N/A"
            r = await c.post("/taobao/top/api", json=_taobao_update())
            assert r.json()["item_sku_price_update_response"]["data"]["price"] == "89.00"

    asyncio.run(_run())


def test_verify_mismatch_on_new_platform():
    """回查不一致脚本：快照返回的价格与目标价不同（data.price 被篡改）。"""

    async def _run():
        fault_injection.load_script("verify_mismatch")
        async with _client() as c:
            r = await c.post("/taobao/top/api", json={
                "method": "taobao.item.sku.get", "app_key": "k", "session": "s",
                "timestamp": "1", "format": "json", "v": "2.0", "sign_method": "hmac-sha256",
                "sign": "X", "shop_id": "SHOP-01", "num_iid": "ITEM-1001", "sku_id": "SKU-002",
            })
            body = r.json()["item_sku_get_response"]["data"]
            assert body["price"] != "89.00"  # 被篡改，回查将判定不一致

    asyncio.run(_run())


def test_verify_timeout_on_new_platform():
    """回查超时脚本：快照请求在副作用后挂起，客户端超时。"""

    async def _run():
        fault_injection.load_script("verify_timeout", timeout_seconds=1.0)
        async with _short_client() as c:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    c.post("/taobao/top/api", json={
                        "method": "taobao.item.sku.get", "app_key": "k", "session": "s",
                        "timestamp": "1", "format": "json", "v": "2.0", "sign_method": "hmac-sha256",
                        "sign": "X", "shop_id": "SHOP-01", "num_iid": "ITEM-1001", "sku_id": "SKU-002",
                    }),
                    timeout=0.3,
                )

    asyncio.run(_run())
