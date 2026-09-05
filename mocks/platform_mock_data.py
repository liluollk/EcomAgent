"""Mock 平台业务数据单源 — 供渠道 Mock 函数与 mock 平台服务（REST 端点）共用。

业务数据模拟口径与渠道工具一致（库存 / 价格 / 订单 / 促销 / 售后 / 知识）。
真实项目：换真实渠道时此模块被真实平台 API 响应取代，Runtime 侧无感知。
"""

import time as _time

# 各渠道商品库存（渠道维度不同字段，商品域为女装——与项目定位一致）
INVENTORY = {
    "taobao": {"name": "海洋之风法式泡泡袖连衣裙", "stock": 1523, "channel_cn": "淘宝"},
    "jd": {"name": "海洋之风高腰 A 字半身裙", "stock": 890, "channel_cn": "京东", "warehouse": "北京仓"},
    "douyin": {"name": "海洋之风复古针织开衫", "stock": 2340, "channel_cn": "抖音"},
}

# 订单状态（渠道维度）
ORDER_STATUS = {
    "taobao": "已发货",
    "jd": "派送中",
    "douyin": "待发货",
}

# 订单/销售分析指标（渠道维度）
ORDER_STATS = {
    "taobao": {"orders": 1280, "gmv": 85600.0, "avg": 66.9},
    "jd": {"orders": 642, "gmv": 51360.0, "avg": 80.0},
    "douyin": {"orders": 2310, "gmv": 120800.0, "avg": 52.3},
}

# 经营异常项（渠道维度）
ANOMALIES = {
    "taobao": ["价格低于成本价（SKU-009）", "库存预警（SKU-017 低于安全水位）"],
    "jd": ["无限 SKU 差评集中（SKU-003）"],
    "douyin": [],
}

# 进行中的促销活动（渠道维度）
PROMOTIONS = {
    "taobao": [{"name": "双11预热 9折", "discount": 0.9}, {"name": "满300减50", "discount": 0.83}],
    "jd": [{"name": "Plus 会员价 95折", "discount": 0.95}],
    "douyin": [{"name": "直播间秒杀 7折", "discount": 0.7}],
}

# 售后分析指标（渠道维度）
AFTER_SALES_STATS = {
    "taobao": {"refund_rate": 0.021, "tickets": 27},
    "jd": {"refund_rate": 0.015, "tickets": 9},
    "douyin": {"refund_rate": 0.038, "tickets": 88},
}

# 经营知识条目（平台级，按主题关键词匹配）
KNOWLEDGE = {
    "话术": "售后安抚话术：先致歉共情，再给方案与时效承诺，最后回访闭环。",
    "上架规范": "上架前核对主图 5 张、详情页合规、价格不低于成本预警线。",
    "退款政策": "仅退款适用于未发货订单；已发货走退货退款流程，48 小时内处理。",
    "发货时效": "现货 24 小时内发货，预售按页面承诺时效，超时自动赔付。",
}

# 校验用渠道白名单
VALID_CHANNELS = frozenset({"taobao", "jd", "douyin"})


def _sales_trend(base_gmv: float, base_orders: int, weekend_lift: float) -> list[dict]:
    """确定性生成近 7 天销售序列（无随机量，可复现）：近两日按周末系数上翘。"""
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


# 近 7 天销售趋势（渠道维度；真实项目由平台统计接口提供）
SALES_TREND = {
    "taobao": _sales_trend(ORDER_STATS["taobao"]["gmv"], ORDER_STATS["taobao"]["orders"], 0.30),
    "jd": _sales_trend(ORDER_STATS["jd"]["gmv"], ORDER_STATS["jd"]["orders"], 0.20),
    "douyin": _sales_trend(ORDER_STATS["douyin"]["gmv"], ORDER_STATS["douyin"]["orders"], 0.45),
}


def match_knowledge(topic: str) -> tuple[str, str] | None:
    """按主题关键词匹配知识条目，未命中返回 None。

    Returns:
        (key, text) | None: 命中时返回 (条目名, 条目文本)。
    """
    for key, text in KNOWLEDGE.items():
        if key in topic:
            return key, text
    return None