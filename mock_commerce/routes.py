"""Mock Commerce API 路由 — FastAPI 应用，独立运行的第三方电商服务模拟。

模拟真实第三方 API 的：认证（X-Api-Key）、错误码信封、限流、延迟、
状态、幂等（X-Idempotency-Key）与确定性故障注入（mock_commerce.fault_injection）。
所有成功响应为 {code, message, data} 信封；错误经 HTTPException 携带
平台业务码（10001-10005/99999）。
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel

from mock_commerce.auth import verify_api_key
from mock_commerce.domain import check_channel, idempotent_call, ok_response
from mock_commerce.fault_injection import (
    FaultOutcome,
    apply_fault_post,
    apply_fault_post_platform,
    apply_fault_pre,
    apply_fault_pre_platform,
    consume_skip_write,
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
    get_product,
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
    row = (
        {key: value for key, value in INVENTORY.get(channel, {}).items() if key != "cost_price"}
        if sku in KNOWN_SKUS
        else {}
    )
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


# ---------------------------------------------------------------------------
# 双平台调价协议形态（Task 3/5）
#
# 淘宝：单端点 POST /taobao/top/api，按信封 method 分发；参数信封 + HMAC-SHA256 签名；
#      价格以「元·两位小数字符串」收发；成功用 item_sku_*_response 信封，错误用 error_response。
# 抖店：JSON 请求体 + access_token；价格以「分·整数」收发；成功用 {code,msg,data}，
#      错误用 {code,msg}（code!=0）。
# 两者共享同一份 PRODUCT_STATE（价格/库存/活动），写操作统一落 record_write 副作用日志。
# ---------------------------------------------------------------------------


def _tb_snapshot_data(p: dict, *, mismatch: bool = False) -> dict:
    price_fen = p["price_fen"] + (100 if mismatch else 0)
    return {
        "price": f"{(Decimal(price_fen) / 100):.2f}",
        "num": p["stock"],
        "status": p["status"],
        "activity": p["activity_name"],
        "activity_locked": p["activity_locked"],
    }


def _tb_ok(response_key: str, data: dict, *, idempotent_replay: bool = False) -> dict:
    body = {"code": 0, "msg": "ok", "data": data}
    if idempotent_replay:
        body["data"] = {**data, "idempotent_replay": True}
    return {response_key: body}


def _tb_error(code: int, msg: str, sub_code: str = "") -> dict:
    return {"error_response": {"code": code, "sub_code": sub_code, "msg": msg}}


def _dy_snapshot_data(p: dict, *, mismatch: bool = False) -> dict:
    price_fen = p["price_fen"] + (100 if mismatch else 0)
    return {
        "price": price_fen,
        "stock": p["stock"],
        "status": p["status"],
        "promotion": p["activity_name"],
        "promotion_locked": p["activity_locked"],
    }


def _dy_ok(data: dict, *, idempotent_replay: bool = False) -> dict:
    body = {"code": 0, "msg": "success", "data": data}
    if idempotent_replay:
        body["data"] = {**data, "idempotent_replay": True}
    return body


def _dy_error(code: int, msg: str) -> dict:
    return {"code": code, "msg": msg}


@app.post("/taobao/top/api")
async def taobao_top_api(request: Request, x_idempotency_key: str | None = Header(None)):
    body = await request.json()
    method = body.get("method")
    fault = await apply_fault_pre_platform("taobao")
    if fault.http_status:
        raise HTTPException(status_code=fault.http_status, detail={"code": fault.code, "msg": fault.msg})
    if fault.kind == "malformed":
        suffix = "item_sku_get_response" if method == "taobao.item.sku.get" else "item_sku_price_update_response"
        return _tb_ok(suffix, {"price": "N/A", "num": "bad", "status": None})
    if fault.kind == "error":
        return _tb_error(fault.code, fault.msg)
    if method == "taobao.item.sku.get":
        p = get_product(body.get("shop_id"), body.get("num_iid"), body.get("sku_id"))
        if p is None:
            return _tb_error(27, "商品不存在")
        data = _tb_snapshot_data(p, mismatch=(fault.kind == "verify_mismatch"))
        await apply_fault_post_platform()
        return _tb_ok("item_sku_get_response", data)
    if method == "taobao.item.sku.price.update":
        p = get_product(body.get("shop_id"), body.get("num_iid"), body.get("sku_id"))
        if p is None:
            return _tb_error(27, "商品不存在")
        if consume_skip_write():
            await apply_fault_post_platform()
            return _tb_ok("item_sku_price_update_response", _tb_snapshot_data(p))
        if p["activity_locked"]:
            return _tb_error(41, "活动锁价，不可改价")
        try:
            new_price_fen = int((Decimal(str(body.get("price"))) * 100).to_integral_value())
        except (InvalidOperation, TypeError, ValueError):
            return _tb_error(15, "参数非法：价格格式错误")
        if new_price_fen < p["cost_fen"]:
            return _tb_error(40, "价格低于成本价，平台拒绝")
        def _data():
            p["price_fen"] = new_price_fen
            record_write("taobao_price_update", {
                "shop_id": body.get("shop_id"), "num_iid": body.get("num_iid"),
                "sku_id": body.get("sku_id"), "price": body.get("price"),
            })
            return _tb_snapshot_data(p)
        inner = idempotent_call(_data, x_idempotency_key)
        replay = bool(inner.get("data", {}).get("idempotent_replay", False))
        await apply_fault_post_platform()
        return _tb_ok("item_sku_price_update_response", inner["data"], idempotent_replay=replay)
    return _tb_error(15, f"未知 method: {method}")


@app.post("/douyin/product/sku/get")
async def douyin_sku_get(request: Request, x_idempotency_key: str | None = Header(None)):
    body = await request.json()
    fault = await apply_fault_pre_platform("douyin")
    if fault.http_status:
        raise HTTPException(status_code=fault.http_status, detail={"code": fault.code, "msg": fault.msg})
    if fault.kind == "malformed":
        return _dy_ok({"price": "oops", "stock": "bad"})
    if fault.kind == "error":
        return _dy_error(fault.code, fault.msg)
    p = get_product(body.get("shop_id"), body.get("product_id"), body.get("sku_id"))
    if p is None:
        return _dy_error(40010, "商品不存在")
    data = _dy_snapshot_data(p, mismatch=(fault.kind == "verify_mismatch"))
    await apply_fault_post_platform()
    return _dy_ok(data)


@app.post("/douyin/product/sku/price")
async def douyin_sku_price(request: Request, x_idempotency_key: str | None = Header(None)):
    body = await request.json()
    fault = await apply_fault_pre_platform("douyin")
    if fault.http_status:
        raise HTTPException(status_code=fault.http_status, detail={"code": fault.code, "msg": fault.msg})
    if fault.kind == "malformed":
        return _dy_ok({"price": "oops", "stock": "bad"})
    if fault.kind == "error":
        return _dy_error(fault.code, fault.msg)
    p = get_product(body.get("shop_id"), body.get("product_id"), body.get("sku_id"))
    if p is None:
        return _dy_error(40010, "商品不存在")
    if consume_skip_write():
        await apply_fault_post_platform()
        return _dy_ok(_dy_snapshot_data(p))
    if p["activity_locked"]:
        return _dy_error(30002, "活动锁价，不可改价")
    try:
        new_price_fen = int(body.get("price"))
    except (TypeError, ValueError):
        return _dy_error(40001, "参数错误：价格格式错误")
    if new_price_fen < p["cost_fen"]:
        return _dy_error(30001, "价格不合规：低于成本价")
    def _data():
        p["price_fen"] = new_price_fen
        record_write("douyin_price_update", {
            "shop_id": body.get("shop_id"), "product_id": body.get("product_id"),
            "sku_id": body.get("sku_id"), "price": new_price_fen,
        })
        return _dy_snapshot_data(p)
    inner = idempotent_call(_data, x_idempotency_key)
    replay = bool(inner.get("data", {}).get("idempotent_replay", False))
    await apply_fault_post_platform()
    return _dy_ok(inner["data"], idempotent_replay=replay)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Mock Commerce API (REST)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
