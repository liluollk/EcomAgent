"""平台侧成本硬闸 — 成本真相以平台数据为准，不信任调用方传入的 cost_price。"""

import asyncio

import httpx
import pytest

from mock_commerce.auth import MOCK_API_KEY
from mock_commerce.domain import reset_idempotency
from mock_commerce.routes import app
from mock_commerce.store import reset_writes


@pytest.fixture(autouse=True)
def _reset():
    reset_writes()
    reset_idempotency()
    yield
    reset_writes()
    reset_idempotency()


def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers={"X-Api-Key": MOCK_API_KEY},
    )


def test_inventory_exposes_cost_price():
    """库存结果携带成本价（真实模型的数据面）。"""

    async def _run():
        async with _client() as c:
            r = await c.get("/v1/taobao/inventory", params={"sku": "SKU-001"})
            assert r.json()["data"]["cost_price"] == 59.0

    asyncio.run(_run())


def test_price_below_platform_cost_rejected_even_if_caller_lies():
    """调用方谎报 cost_price=1，平台仍按真实成本 59 拒绝（409 / 10004）。"""

    async def _run():
        async with _client() as c:
            r = await c.put(
                "/v1/taobao/price",
                json={"sku": "SKU-001", "new_price": 20.0, "cost_price": 1.0},
            )
            assert r.status_code == 409
            assert r.json()["detail"]["code"] == 10004
            assert "成本" in r.json()["detail"]["message"]

    asyncio.run(_run())


def test_price_above_cost_allowed():
    async def _run():
        async with _client() as c:
            r = await c.put("/v1/taobao/price", json={"sku": "SKU-001", "new_price": 89.0})
            assert r.status_code == 200
            assert r.json()["data"]["new_price"] == 89.0

    asyncio.run(_run())


def test_unknown_sku_no_cost_gate():
    """未知 SKU 无成本数据，不触发成本闸（交由业务层处理）。"""

    async def _run():
        async with _client() as c:
            r = await c.put("/v1/taobao/price", json={"sku": "SKU-999", "new_price": 1.0})
            assert r.status_code == 200

    asyncio.run(_run())
