"""Mock 平台业务数据存储 — 库存/订单/促销/售后/知识等业务数据。"""

import time as _time

INVENTORY = {
    "taobao": {"name": "海洋之风法式泡泡袖连衣裙", "stock": 1523, "cost_price": 59.0, "channel_cn": "淘宝"},
    "jd": {"name": "海洋之风高腰 A 字半身裙", "stock": 890, "cost_price": 120.0, "channel_cn": "京东", "warehouse": "北京仓"},
    "douyin": {"name": "海洋之风复古针织开衫", "stock": 2340, "cost_price": 45.0, "channel_cn": "抖音"},
}

ORDER_STATUS = {
    "taobao": "已发货",
    "jd": "派送中",
    "douyin": "待发货",
}

ORDER_STATS = {
    "taobao": {"orders": 1280, "gmv": 85600.0, "avg": 66.9},
    "jd": {"orders": 642, "gmv": 51360.0, "avg": 80.0},
    "douyin": {"orders": 2310, "gmv": 120800.0, "avg": 52.3},
}

ANOMALIES = {
    "taobao": ["价格低于成本价（SKU-009）", "库存预警（SKU-017 低于安全水位）"],
    "jd": ["无限 SKU 差评集中（SKU-003）"],
    "douyin": [],
}

PROMOTIONS = {
    "taobao": [{"name": "双11预热 9折", "discount": 0.9}, {"name": "满300减50", "discount": 0.83}],
    "jd": [{"name": "Plus 会员价 95折", "discount": 0.95}],
    "douyin": [{"name": "直播间秒杀 7折", "discount": 0.7}],
}

AFTER_SALES_STATS = {
    "taobao": {"refund_rate": 0.021, "tickets": 27},
    "jd": {"refund_rate": 0.015, "tickets": 9},
    "douyin": {"refund_rate": 0.038, "tickets": 88},
}

KNOWLEDGE = {
    "话术": "售后安抚话术：先致歉共情，再给方案与时效承诺，最后回访闭环。",
    "上架规范": "上架前核对主图 5 张、详情页合规、价格不低于成本预警线。",
    "退款政策": "仅退款适用于未发货订单；已发货走退货退款流程，48 小时内处理。",
    "发货时效": "现货 24 小时内发货，预售按页面承诺时效，超时自动赔付。",
}

VALID_CHANNELS = frozenset({"taobao", "jd", "douyin"})

# 已知 SKU：未知 SKU 的库存查询返回空行（优雅降级契约，对应 harness 场景 unknown_sku_graceful）
KNOWN_SKUS = frozenset({"SKU-001", "SKU-002", "SKU-003"})

# ---------------------------------------------------------------------------
# 写副作用审计日志 — 验证幂等/重试语义：「同一次逻辑写只落一次副作用」
# ---------------------------------------------------------------------------

WRITE_LOG: list[dict] = []


def record_write(op: str, data: dict) -> None:
    """记录一次真实应用的写副作用（幂等回放不记录）。"""
    WRITE_LOG.append({"op": op, "data": dict(data)})


def writes_of(op: str) -> list[dict]:
    """返回某类写操作的全部真实副作用记录。"""
    return [w for w in WRITE_LOG if w["op"] == op]


def reset_writes() -> None:
    """清空写副作用日志（场景隔离用）。"""
    WRITE_LOG.clear()


def _sales_trend(base_gmv: float, base_orders: int, weekend_lift: float) -> list[dict]:
    rows: list[dict] = []
    now = _time.time()
    for i in range(7):
        ts = now - (6 - i) * 86400
        factor = 1.0 + (weekend_lift if i >= 5 else -0.06 * ((6 - i) % 3))
        rows.append(
            {
                "date": _time.strftime("%m-%d", _time.localtime(ts)),
                "gmv": round(base_gmv * factor / 7, 2),
                "orders": max(1, round(base_orders * factor / 7)),
            }
        )
    return rows


SALES_TREND = {
    "taobao": _sales_trend(ORDER_STATS["taobao"]["gmv"], ORDER_STATS["taobao"]["orders"], 0.30),
    "jd": _sales_trend(ORDER_STATS["jd"]["gmv"], ORDER_STATS["jd"]["orders"], 0.20),
    "douyin": _sales_trend(ORDER_STATS["douyin"]["gmv"], ORDER_STATS["douyin"]["orders"], 0.45),
}


def match_knowledge(topic: str) -> tuple[str, str] | None:
    for key, text in KNOWLEDGE.items():
        if key in topic:
            return key, text
    return None


def get_cost_price(channel: str, sku: str) -> float | None:
    """平台成本真相访问器：规则引擎据此判定成本保护，不信任调用方传入值。

    这是「平台持有的数据」的窄接口——真实平台下它对应一次异步查询/缓存，
    mock 在进程内同步返回。未知 SKU 无成本数据返回 None（交由业务层处理）。
    """
    if sku not in KNOWN_SKUS:
        return None
    return (INVENTORY.get(channel) or {}).get("cost_price")