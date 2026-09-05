"""工作台数据路由 — 聚合各渠道运营指标（真实协议层取数）。

数据来源：每个启用渠道经其 REST client（真实 HTTP 往返）请求平台服务的
只读端点（order-stats / promotions / after-sales-stats / anomalies /
inventory），前端工作台「总览 / 促销」分区直接消费，替代静态演示数据。

容错口径：
  - 单渠道单端点失败不阻塞整体（该字段置 None / 空列表）；
  - 核心指标 order-stats 失败 → 该渠道 connected=False 并带 error 说明；
  - 真实平台渠道（platform != mock）适配层未接入，返回占位并注明，
    不向真实网关发请求。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from starlette.responses import JSONResponse

from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

router = APIRouter()

# inventory 为按 SKU 查询端点，聚合视图取默认演示 SKU（mock 数据为渠道级）
_DEFAULT_SKU = "SKU-001"


async def _safe(coro):
    """单端点取数容错：失败返回 None，不阻塞同渠道其他端点。"""
    try:
        return await coro
    except Exception:
        return None


async def _fetch_channel(client, name: str) -> dict:
    """并发拉取单渠道五类只读数据。"""
    order_stats, promos, after_sales, anomalies, inventory = await asyncio.gather(
        _safe(client.call("GET", f"/v1/{name}/order-stats", params={"period": "近7天"})),
        _safe(client.call("GET", f"/v1/{name}/promotions")),
        _safe(client.call("GET", f"/v1/{name}/after-sales-stats", params={"period": "近7天"})),
        _safe(client.call("GET", f"/v1/{name}/anomalies")),
        _safe(client.call("GET", f"/v1/{name}/inventory", params={"sku": _DEFAULT_SKU})),
    )
    product = None
    if isinstance(inventory, dict) and inventory.get("name"):
        product = {
            "sku": inventory.get("sku") or _DEFAULT_SKU,
            "name": inventory.get("name"),
            "stock": inventory.get("stock"),
        }
    return {
        "connected": isinstance(order_stats, dict),
        "orders": (order_stats or {}).get("orders"),
        "gmv": (order_stats or {}).get("gmv"),
        "avg_order": (order_stats or {}).get("avg"),
        "refund_rate": (after_sales or {}).get("refund_rate"),
        "tickets": (after_sales or {}).get("tickets"),
        "promotions": (promos or {}).get("items") or [],
        "anomalies": (anomalies or {}).get("items") or [],
        "product": product,
    }


@router.get("/workspace/overview")
async def workspace_overview() -> JSONResponse:
    """聚合全部启用渠道的运营指标（供前端工作台总览/促销分区）。"""
    channels_out: list[dict] = []
    for name in sorted(DEFAULT_CHANNEL_REGISTRY.enabled_names()):
        cfg = DEFAULT_CHANNEL_REGISTRY.get(name) or {}
        platform = str(cfg.get("platform") or "mock")
        row: dict = {
            "name": name,
            "label": cfg.get("label") or name,
            "platform": platform,
            "enabled": bool(cfg.get("enabled", True)),
            "connected": False,
            "error": None,
            "orders": None,
            "gmv": None,
            "avg_order": None,
            "refund_rate": None,
            "tickets": None,
            "promotions": [],
            "anomalies": [],
            "product": None,
        }
        if platform != "mock":
            row["error"] = "真实平台适配器未接入（需平台资质）"
            channels_out.append(row)
            continue
        client = DEFAULT_CHANNEL_REGISTRY.client_for(name)
        if client is None:
            row["error"] = "渠道客户端不可用"
            channels_out.append(row)
            continue
        data = await _fetch_channel(client, name)
        row.update(data)
        if not data["connected"]:
            row["error"] = "平台数据源不可达"
        channels_out.append(row)

    connected = [c for c in channels_out if c["connected"]]
    return JSONResponse(
        content={
            "channels": channels_out,
            "summary": {
                "total_channels": len(channels_out),
                "connected_channels": len(connected),
                "total_orders": sum(c["orders"] or 0 for c in connected),
                "total_gmv": sum(c["gmv"] or 0.0 for c in connected),
                "total_promotions": sum(len(c["promotions"]) for c in connected),
                "total_anomalies": sum(len(c["anomalies"]) for c in connected),
            },
        }
    )