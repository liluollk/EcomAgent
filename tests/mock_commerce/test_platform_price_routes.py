"""Mock 双平台调价路由测试（Task 5）— 淘宝/抖店协议形态不同，但驱动同一份商品状态。

这些测试直接打 in-process ASGI（mock_commerce.routes.app），验证：
  - 淘宝(TOP 信封) / 抖店(JSON 信封) 请求字段与响应信封明显不同；
  - 两者读写同一份 PRODUCT_STATE（价格/库存/活动），副作用落同一份写日志；
  - 成本价只来自平台数据，低于成本的写入被拒绝；活动锁价拒绝改价；
  - 写操作幂等（X-Idempotency-Key）且仅落一次副作用。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from mock_commerce import fault_injection
from mock_commerce.auth import MOCK_API_KEY
from mock_commerce.domain import reset_idempotency
from mock_commerce.routes import app
from mock_commerce.store import (
    get_product,
    reset_product_state,
    reset_writes,
    writes_of,
)


@pytest.fixture(autouse=True)
def _reset():
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    reset_product_state()
    yield
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    reset_product_state()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"X-Api-Key": MOCK_API_KEY},
    )


def _taobao_env(method: str, **fields) -> dict:
    base = {
        "method": method,
        "app_key": "mock_app_key",
        "session": "mock_session",
        "timestamp": "1700000000",
        "format": "json",
        "v": "2.0",
        "sign_method": "hmac-sha256",
        "sign": "DEADBEEF",
        "shop_id": "SHOP-01",
        "num_iid": "ITEM-1001",
        "sku_id": "SKU-002",
    }
    base.update(fields)
    return base


def _douyin_env(**fields) -> dict:
    base = {
        "access_token": "mock_access_token",
        "app_key": "mock_app_key",
        "sign": "DEADBEEF",
        "timestamp": 1700000000,
        "shop_id": "SHOP-01",
        "product_id": "ITEM-1001",
        "sku_id": "SKU-002",
    }
    base.update(fields)
    return base


def test_taobao_envelope_shape_and_unit():
    async def run():
        async with _client() as c:
            r = await c.post("/taobao/top/api", json=_taobao_env("taobao.item.sku.get"))
            assert r.status_code == 200
            body = r.json()
            assert "item_sku_get_response" in body
            data = body["item_sku_get_response"]["data"]
            # 淘宝以「元·两位小数字符串」表达价格
            assert data["price"] == "89.00"
            assert "activity_locked" in data

    asyncio.run(run())


def test_douyin_envelope_shape_and_unit():
    async def run():
        async with _client() as c:
            r = await c.post("/douyin/product/sku/get", json=_douyin_env())
            assert r.status_code == 200
            body = r.json()
            # 抖店顶层 {code,msg,data}，无嵌套 method 键
            assert body["code"] == 0
            assert "item_sku_get_response" not in body
            # 抖店以「分·整数」表达价格
            assert body["data"]["price"] == 8900
            assert "promotion_locked" in body["data"]

    asyncio.run(run())


def test_shared_state_between_platforms():
    """同一个改价经过淘宝落到 PRODUCT_STATE，抖店快照应能读到——证明共享状态。"""
    async def run():
        async with _client() as c:
            # 淘宝把 SKU-002 改到 99.00 元
            await c.post(
                "/taobao/top/api",
                json=_taobao_env("taobao.item.sku.price.update", price="99.00"),
                headers={"X-Idempotency-Key": "share-1"},
            )
            # 抖店快照应读到 99.00 元 = 9900 分
            r = await c.post("/douyin/product/sku/get", json=_douyin_env())
            assert r.json()["data"]["price"] == 9900
            # 底层共享状态对象一致（淘宝/抖店共享同一份）
            assert get_product("SHOP-01", "ITEM-1001", "SKU-002")["price_fen"] == 9900
            assert get_product("SHOP-01", "ITEM-1001", "SKU-002")["price_fen"] == 9900

    asyncio.run(run())


def test_below_cost_rejected_by_platform():
    async def run():
        async with _client() as c:
            r = await c.post(
                "/taobao/top/api",
                json=_taobao_env("taobao.item.sku.price.update", price="10.00"),
            )
            assert r.json()["error_response"]["code"] == 40
            r = await c.post("/douyin/product/sku/price", json=_douyin_env(price=1000))
            assert r.json()["code"] == 30001

    asyncio.run(run())


def test_activity_lock_rejected():
    async def run():
        async with _client() as c:
            r = await c.post(
                "/taobao/top/api",
                json={**_taobao_env("taobao.item.sku.price.update", price="99.00"), "sku_id": "SKU-001"},
            )
            assert r.json()["error_response"]["code"] == 41
            r = await c.post("/douyin/product/sku/price", json={**_douyin_env(price=9900), "sku_id": "SKU-001"})
            assert r.json()["code"] == 30002

    asyncio.run(run())


def test_write_side_effect_logged_once_with_idempotency():
    async def run():
        async with _client() as c:
            body = _taobao_env("taobao.item.sku.price.update", price="99.00")
            r1 = await c.post("/taobao/top/api", json=body, headers={"X-Idempotency-Key": "k-tao-1"})
            assert r1.json()["item_sku_price_update_response"]["data"].get("idempotent_replay") is not True
            r2 = await c.post("/taobao/top/api", json=body, headers={"X-Idempotency-Key": "k-tao-1"})
            assert r2.json()["item_sku_price_update_response"]["data"].get("idempotent_replay") is True
            # 副作用只落一次
            assert len(writes_of("taobao_price_update")) == 1

            body2 = _douyin_env(price=9900)
            await c.post("/douyin/product/sku/price", json=body2, headers={"X-Idempotency-Key": "k-dy-1"})
            assert len(writes_of("douyin_price_update")) == 1

    asyncio.run(run())


def test_capability_refused_script_returns_platform_error():
    async def run():
        fault_injection.load_script("capability_refused")
        async with _client() as c:
            r = await c.post(
                "/taobao/top/api",
                json=_taobao_env("taobao.item.sku.price.update", price="99.00"),
            )
            assert r.json()["error_response"]["code"] == 9998
            # 每个平台请求独立消费一步脚本，分开加载以保证两侧都命中
            fault_injection.load_script("capability_refused")
            r = await c.post("/douyin/product/sku/price", json=_douyin_env(price=9900))
            assert r.json()["code"] == 90000

    asyncio.run(run())
