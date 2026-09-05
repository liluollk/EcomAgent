"""运营看板路由 — 时间序列与跨渠道汇总聚合（真实协议层取数）。

与工作台总览（/workspace/overview，渠道维度明细）互补：看板聚焦
「时间维度」——近 7 天销售趋势（sales-trend）+ 跨渠道汇总 + 库存预警聚合。
数据来源与容错口径与 workspace_data 一致：每启用渠道经 REST client 并发
取数，单渠道失败不阻塞整体；真实平台渠道返回占位（适配层未接入）。
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


@router.get("/dashboard/summary")
async def dashboard_summary() -> JSONResponse:
    """聚合运营看板数据：概览指标 + 近 7 天趋势 + 渠道水位 + 预警列表。"""
    channels_out: list[dict] = []
    trend_by_date: dict[str, dict] = {}
    alerts: list[dict] = []

    for name in sorted(DEFAULT_CHANNEL_REGISTRY.enabled_names()):
        cfg = DEFAULT_CHANNEL_REGISTRY.get(name) or {}
        platform = str(cfg.get("platform") or "mock")
        label = cfg.get("label") or name
        row: dict = {
            "name": name,
            "label": label,
            "platform": platform,
            "connected": False,
            "gmv": None,
            "orders": None,
            "stock": None,
            "product": None,
            "anomalies": [],
        }
        if platform != "mock":
            row["anomalies"] = ["真实平台适配器未接入（需平台资质）"]
            channels_out.append(row)
            continue
        client = DEFAULT_CHANNEL_REGISTRY.client_for(name)
        if client is None:
            row["anomalies"] = ["渠道客户端不可用"]
            channels_out.append(row)
            continue

        trend, inventory, anomalies, order_stats = await asyncio.gather(
            _safe(client.call("GET", f"/v1/{name}/sales-trend", params={"days": 7})),
            _safe(client.call("GET", f"/v1/{name}/inventory", params={"sku": _DEFAULT_SKU})),
            _safe(client.call("GET", f"/v1/{name}/anomalies")),
            _safe(client.call("GET", f"/v1/{name}/order-stats", params={"period": "近7天"})),
        )
        connected = isinstance(order_stats, dict)
        row.update(
            {
                "connected": connected,
                "gmv": (order_stats or {}).get("gmv"),
                "orders": (order_stats or {}).get("orders"),
                "stock": (inventory or {}).get("stock"),
                "product": (inventory or {}).get("name"),
                "anomalies": (anomalies or {}).get("items") or [],
            }
        )
        for a in row["anomalies"]:
            alerts.append({"channel": label, "text": a})
        channels_out.append(row)

        # 按日期归并各渠道趋势为全渠道汇总序列
        for trow in (trend or {}).get("trend") or []:
            slot = trend_by_date.setdefault(
                trow["date"], {"date": trow["date"], "gmv": 0.0, "orders": 0}
            )
            slot["gmv"] = round(slot["gmv"] + (trow.get("gmv") or 0.0), 2)
            slot["orders"] += trow.get("orders") or 0

    connected = [c for c in channels_out if c["connected"]]
    trend = [trend_by_date[key] for key in sorted(trend_by_date)]
    return JSONResponse(
        content={
            "summary": {
                "total_gmv": round(sum(c["gmv"] or 0.0 for c in connected), 2),
                "total_orders": sum(c["orders"] or 0 for c in connected),
                "total_stock": sum(c["stock"] or 0 for c in connected),
                "connected_channels": len(connected),
                "alert_count": len(alerts),
            },
            "trend": trend,
            "channels": channels_out,
            "alerts": alerts,
        }
    )
