"""Mock Commerce API 路由 — FastAPI 应用，独立运行的第三方电商服务模拟。

模拟真实第三方 API 的：认证（X-Api-Key）、错误码信封、限流、延迟、
状态、幂等（X-Idempotency-Key）与确定性故障注入（mock_commerce.fault_injection）。
所有成功响应为 {code, message, data} 信封；错误经 HTTPException 携带
平台业务码（10001-10005/99999）。
"""

from __future__ import annotations

import argparse

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from mock_commerce.auth import verify_api_key
from mock_commerce.domain import check_channel, idempotent_call, ok_response
from mock_commerce.fault_injection import (
    FaultOutcome,
    apply_fault_post,
    apply_fault_pre,
    request_method_var,
)
from mock_commerce.store import (
    AFTER_SALES_STATS,
    ANOMALIES,
    INVENTORY,
    KNOWN_SKUS,
    ORDER_STATS,
    ORDER_STATUS,
    PROMOTIONS,
    SALES_TREND,
    match_knowledge,
    record_write,
)

app = FastAPI(title="Mock Commerce API", version="2.0.0")


@app.middleware("http")
async def _capture_request_method(request: Request, call_next):
    """把当前请求的 HTTP 方法写入 contextvar，供故障脚本定向消费判定。"""
    token = request_method_var.set(request.method)
    try:
        return await call_next(request)
    finally:
        request_method_var.reset(token)


@app.get("/v1/{channel}/inventory")
async def inventory(channel: str, sku: str, x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    # 未知 SKU → 空库存行（优雅降级契约）；已知 SKU 返回渠道库存数据
    row = dict(INVENTORY.get(channel, {})) if sku in KNOWN_SKUS else {}
    await apply_fault_post()
    return ok_response({"channel": channel, "sku": sku, **row})


class PriceBody(BaseModel):
    sku: str
    new_price: float
    cost_price: float = 0


@app.put("/v1/{channel}/price")
async def update_price(
    channel: str,
    body: PriceBody,
    x_api_key: str | None = Header(None),
    x_idempotency_key: str | None = Header(None),
):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)

    # 平台真相：成本价以平台数据为准，不信任调用方传入的 cost_price——
    # 绕过 PreToolUse 早闸（如真实模型未传 cost_price）的直接写请求，在这里被拒。
    # 仅对已知 SKU 生效：未知 SKU 无成本数据，交由业务层处理
    platform_cost = (INVENTORY.get(channel) or {}).get("cost_price") if body.sku in KNOWN_SKUS else None
    if platform_cost is not None and body.new_price < platform_cost:
        raise HTTPException(
            status_code=409,
            detail={"code": 10004, "message": f"价格低于成本价 {platform_cost}，不允许调整"},
        )

    def _data() -> dict:
        # 副作用在构造时发生（写审计日志），之后才可能被 post 阶段的 timeout 挂起
        record_write("update_price", {"channel": channel, "sku": body.sku, "new_price": body.new_price})
        return {"channel": channel, "sku": body.sku, "new_price": body.new_price}

    resp = idempotent_call(_data, x_idempotency_key)
    await apply_fault_post()
    return resp


class PromotionBody(BaseModel):
    sku: str
    discount: float
    start_time: str
    end_time: str


@app.post("/v1/{channel}/promotions")
async def create_promotion(
    channel: str,
    body: PromotionBody,
    x_api_key: str | None = Header(None),
    x_idempotency_key: str | None = Header(None),
):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)

    def _data() -> dict:
        payload = {
            "channel": channel,
            "sku": body.sku,
            "discount": body.discount,
            "start_time": body.start_time,
            "end_time": body.end_time,
        }
        record_write("create_promotion", payload)
        return payload

    resp = idempotent_call(_data, x_idempotency_key)
    await apply_fault_post()
    return resp


@app.get("/v1/{channel}/orders/{order_id}")
async def order_status(channel: str, order_id: str, x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    await apply_fault_post()
    return ok_response({"channel": channel, "order_id": order_id, "status": ORDER_STATUS.get(channel, "未知")})


@app.get("/v1/{channel}/order-stats")
async def order_stats(channel: str, period: str = Query("近7天"), x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    d = ORDER_STATS.get(channel, {"orders": 0, "gmv": 0.0, "avg": 0.0})
    await apply_fault_post()
    return ok_response({"channel": channel, "period": period, **d})


@app.get("/v1/{channel}/sales-trend")
async def sales_trend(channel: str, days: int = Query(7, ge=1, le=30), x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    rows = SALES_TREND.get(channel, [])[-days:]
    await apply_fault_post()
    return ok_response({"channel": channel, "days": len(rows), "trend": rows})


@app.get("/v1/{channel}/anomalies")
async def anomalies(channel: str, x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    await apply_fault_post()
    return ok_response({"channel": channel, "items": ANOMALIES.get(channel, [])})


@app.get("/v1/{channel}/promotions")
async def promotions(channel: str, x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    await apply_fault_post()
    return ok_response({"channel": channel, "items": PROMOTIONS.get(channel, [])})


@app.get("/v1/{channel}/after-sales-stats")
async def after_sales_stats(channel: str, period: str = Query("近7天"), x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)
    d = AFTER_SALES_STATS.get(channel, {"refund_rate": 0.0, "tickets": 0})
    await apply_fault_post()
    return ok_response({"channel": channel, "period": period, **d})


class ShelfBody(BaseModel):
    sku: str
    action: str


@app.put("/v1/{channel}/shelf")
async def shelf(
    channel: str,
    body: ShelfBody,
    x_api_key: str | None = Header(None),
    x_idempotency_key: str | None = Header(None),
):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)

    def _data() -> dict:
        payload = {"channel": channel, "sku": body.sku, "action": body.action}
        record_write("product_shelf", payload)
        return payload

    resp = idempotent_call(_data, x_idempotency_key)
    await apply_fault_post()
    return resp


class TicketBody(BaseModel):
    order_id: str
    issue: str
    priority: str = "normal"


@app.post("/v1/{channel}/service-tickets")
async def service_ticket(
    channel: str,
    body: TicketBody,
    x_api_key: str | None = Header(None),
    x_idempotency_key: str | None = Header(None),
):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    check_channel(channel)

    def _data() -> dict:
        payload = {
            "channel": channel,
            "order_id": body.order_id,
            "issue": body.issue,
            "priority": body.priority,
            "ticket_id": f"ST-{body.order_id[-4:]}-{hash(body.issue) % 1000:03d}",
        }
        record_write("service_ticket", payload)
        return payload

    resp = idempotent_call(_data, x_idempotency_key)
    await apply_fault_post()
    return resp


@app.get("/v1/knowledge-base")
async def knowledge_base(topic: str, x_api_key: str | None = Header(None)):
    verify_api_key(x_api_key)
    fault = await apply_fault_pre()
    if fault is FaultOutcome.MALFORMED:
        # 通用畸形成功响应：code=0 信封完好，但 data 含类型违例字段
        # （string 型 stock 触发 Schema 校验；对必填字段操作则缺字段触发）
        return ok_response({"unexpected_field": True, "stock": "N/A"})
    await apply_fault_post()
    matched = match_knowledge(topic)
    if matched is None:
        return ok_response({"topic": topic, "matched": False, "text": None, "key": None})
    key, text = matched
    return ok_response({"topic": topic, "matched": True, "text": text, "key": key})


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock Commerce API (REST)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
