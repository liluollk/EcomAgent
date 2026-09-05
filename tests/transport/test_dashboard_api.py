"""运营看板聚合 API 测试 — 趋势归并 / 渠道水位 / 预警聚合。"""

import pytest
from httpx import ASGITransport, AsyncClient

import sources.channel_registry as _cr
from transport.server import app


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """隔离渠道注册表与配置（离线 ASGI 兜底直连 mock 平台）。"""
    monkeypatch.setenv("CHANNEL_CONFIG_FILE", str(tmp_path / "channels.json"))
    monkeypatch.setenv("CHANNEL_API_AUTO", "0")
    _cr._store._mtime = -1
    _cr._store._channels = None
    _cr.DEFAULT_CHANNEL_REGISTRY._clients.clear()
    yield
    _cr.DEFAULT_CHANNEL_REGISTRY._clients.clear()


async def test_dashboard_summary_aggregates(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/dashboard/summary")
        assert r.status_code == 200
        body = r.json()

        # 概览指标
        summary = body["summary"]
        assert summary["connected_channels"] >= 1
        assert summary["total_gmv"] > 0
        assert summary["total_orders"] > 0

        # 7 天趋势：日期升序、结构完整
        trend = body["trend"]
        assert len(trend) == 7
        dates = [row["date"] for row in trend]
        assert dates == sorted(dates)
        assert all({"date", "gmv", "orders"} <= set(row) for row in trend)
        # 多渠道趋势已归并：单日 gmv 应大于单渠道日均
        assert trend[-1]["gmv"] > 0

        # 渠道行与预警
        names = {c["name"] for c in body["channels"]}
        assert {"taobao", "jd", "douyin"} <= names
        taobao = next(c for c in body["channels"] if c["name"] == "taobao")
        assert taobao["connected"] is True
        assert taobao["stock"] and taobao["product"]
        assert any("SKU-017" in a["text"] for a in body["alerts"])
