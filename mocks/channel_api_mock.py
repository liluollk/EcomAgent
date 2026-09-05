"""Mock 渠道平台服务 — 以 REST 形态暴露渠道业务端点（业务数据 Mock）。

对应真实淘宝 TOP / 京东 JOS / 抖音开放平台的 API 形态：
  - HTTP 方法 + 路径 + JSON 请求/响应（{"code": 0, "data": {...}}）；
  - 鉴权头 X-Api-Key（真实项目换 OAuth access_token）；
  - 平台业务错误码（10001 参数错 / 10002 渠道不存在 / 10003 无权限 / 10004 业务冲突 / 10005 限流）。

本服务是独立进程（uvicorn），由 transport/server.py lifespan 拉起；
换真实渠道时本服务整体退役，Runtime / MCP Server 只改 base_url 与鉴权。

运行方式:
    python -m mocks.channel_api_mock            # 默认 127.0.0.1:18080
    python -m mocks.channel_api_mock --port 8080
"""

from __future__ import annotations

import argparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mocks.platform_mock_data import (
    ANOMALIES,
    AFTER_SALES_STATS,
    INVENTORY,
    KNOWLEDGE,
    ORDER_STATS,
    ORDER_STATUS,
    PROMOTIONS,
    SALES_TREND,
    VALID_CHANNELS,
    match_knowledge,
)

# 模拟鉴权 key（真实项目：OAuth 商家 token）
MOCK_API_KEY = "mock-channel-key"

# 写操作幂等存储（平台进程内存）：key -> 首次处理结果 data
# 真实平台：幂等键为短窗口（秒级~分钟级）缓存，本 mock 用进程内存模拟；
# 重启即清空，与真实平台行为一致（幂等窗口有限）。
_idempotency: dict[str, dict] = {}


def _idempotent_post(data_factory, x_idempotency_key: str | None) -> dict:
    """POST 写操作幂等处理：同 key 重复请求返回首次结果，不重复创建。

    Args:
        data_factory: 生成业务数据的可调用对象（仅首次调用）。
        x_idempotency_key: 幂等键（X-Idempotency-Key 头），None 则不启用幂等。

    Returns:
        dict: 平台响应体（含 idempotent_replay 标记供上层感知）。
    """
    if not x_idempotency_key:
        return _ok(data_factory(), idempotent_replay=False)
    if x_idempotency_key in _idempotency:
        return _ok(_idempotency[x_idempotency_key], idempotent_replay=True)
    result = data_factory()
    _idempotency[x_idempotency_key] = result
    return _ok(result, idempotent_replay=False)


app = FastAPI(title="Mock Channel Platform", version="1.1.0")


def _check_auth(x_api_key: str | None) -> None:
    """模拟平台鉴权：X-Api-Key 必须为 mock key。"""
    if x_api_key != MOCK_API_KEY:
        raise HTTPException(status_code=401, detail={"code": 10003, "message": "鉴权失败：无权限操作"})


def _check_channel(channel: str) -> None:
    """渠道不存在 → 平台业务码 10002（HTTP 404 + 顶层业务体）。

    合法集合 = 内置三渠道 ∪ 渠道注册表中已启用的渠道（支持设置里动态新增渠道），
    新增渠道默认返回通用 mock 数据（各端点已有 .get fallback）。
    """
    from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

    valid = set(VALID_CHANNELS) | DEFAULT_CHANNEL_REGISTRY.enabled_names()
    if channel not in valid:
        raise HTTPException(status_code=404, detail={"code": 10002, "message": f"渠道不存在: {channel}"})


def _ok(data: dict | list | str, idempotent_replay: bool = False) -> dict:
    """平台成功响应体；幂等回放标记内嵌进 data（供上层感知）。"""
    if idempotent_replay and isinstance(data, dict):
        data = {**data, "idempotent_replay": True}
    return {"code": 0, "message": "ok", "data": data}


@app.get("/v1/{channel}/inventory")
async def inventory(channel: str, sku: str, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    row = INVENTORY.get(channel, {})
    return _ok({"channel": channel, "sku": sku, **row})


class PriceBody(BaseModel):
    sku: str
    new_price: float
    cost_price: float = 0


@app.put("/v1/{channel}/price")
async def update_price(channel: str, body: PriceBody, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    return _ok({"channel": channel, "sku": body.sku, "new_price": body.new_price})


class PromotionBody(BaseModel):
    sku: str
    discount: float
    start_time: str
    end_time: str


@app.post("/v1/{channel}/promotions")
async def create_promotion(channel: str, body: PromotionBody,
                           x_api_key: str | None = Header(None),
                           x_idempotency_key: str | None = Header(None)):
    """创建促销（POST 写操作，支持幂等键：同 key 重复请求不重复创建）。"""
    _check_auth(x_api_key)
    _check_channel(channel)

    def _data() -> dict:
        return {
            "channel": channel,
            "sku": body.sku,
            "discount": body.discount,
            "start_time": body.start_time,
            "end_time": body.end_time,
        }

    return _idempotent_post(_data, x_idempotency_key)


@app.get("/v1/{channel}/orders/{order_id}")
async def order_status(channel: str, order_id: str, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    return _ok({"channel": channel, "order_id": order_id, "status": ORDER_STATUS.get(channel, "未知")})


@app.get("/v1/{channel}/order-stats")
async def order_stats(channel: str, period: str = Query("近7天"), x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    d = ORDER_STATS.get(channel, {"orders": 0, "gmv": 0.0, "avg": 0.0})
    return _ok({"channel": channel, "period": period, **d})


@app.get("/v1/{channel}/sales-trend")
async def sales_trend(channel: str, days: int = Query(7, ge=1, le=30), x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    rows = SALES_TREND.get(channel, [])[-days:]
    return _ok({"channel": channel, "days": len(rows), "trend": rows})


@app.get("/v1/{channel}/anomalies")
async def anomalies(channel: str, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    return _ok({"channel": channel, "items": ANOMALIES.get(channel, [])})


@app.get("/v1/{channel}/promotions")
async def promotions(channel: str, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    return _ok({"channel": channel, "items": PROMOTIONS.get(channel, [])})


@app.get("/v1/{channel}/after-sales-stats")
async def after_sales_stats(channel: str, period: str = Query("近7天"), x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    d = AFTER_SALES_STATS.get(channel, {"refund_rate": 0.0, "tickets": 0})
    return _ok({"channel": channel, "period": period, **d})


class ShelfBody(BaseModel):
    sku: str
    action: str  # on=上架 / off=下架


@app.put("/v1/{channel}/shelf")
async def shelf(channel: str, body: ShelfBody, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    _check_channel(channel)
    return _ok({"channel": channel, "sku": body.sku, "action": body.action})


class TicketBody(BaseModel):
    order_id: str
    issue: str
    priority: str = "normal"


@app.post("/v1/{channel}/service-tickets")
async def service_ticket(channel: str, body: TicketBody,
                         x_api_key: str | None = Header(None),
                         x_idempotency_key: str | None = Header(None)):
    """创建售后工单（POST 写操作，支持幂等键）。"""
    _check_auth(x_api_key)
    _check_channel(channel)

    def _data() -> dict:
        ticket_id = f"ST-{body.order_id[-4:]}-{hash(body.issue) % 1000:03d}"
        return {"channel": channel, "order_id": body.order_id, "issue": body.issue,
                "priority": body.priority, "ticket_id": ticket_id}

    return _idempotent_post(_data, x_idempotency_key)


@app.get("/v1/knowledge-base")
async def knowledge_base(topic: str, x_api_key: str | None = Header(None)):
    _check_auth(x_api_key)
    matched = match_knowledge(topic)
    if matched is None:
        return _ok({"topic": topic, "matched": False, "text": None, "key": None})
    key, text = matched
    return _ok({"topic": topic, "matched": True, "text": text, "key": key})


def main() -> None:
    """直接运行 mock 平台服务（供 lifespan 子进程拉起 / 手动调试）。"""
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock channel platform (REST)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()