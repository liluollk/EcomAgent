"""CommerceProvider 实现 — 领域接口的 HTTP 适配落地。

两层结构：
  - invoke_operation：所有电商语义操作的共享执行通道。
    Adapter 翻译请求 → REST 客户端单次调用 → Execution Policy 管控
    （超时 / 错误分类重试 / 幂等键 / Schema+Business 校验）→ 归一化 dict。
    《最终架构》链路中的「Permission → Execution Policy → Adapter」在此收口。
  - HttpCommerceProvider：CommerceProvider Protocol 的实现，
    把操作结果映射为领域结果类型——不向上层暴露 HTTP Response。

Runtime 只依赖 CommerceProvider 接口；将来接入淘宝/京东真实平台时，
替换 provider/client/adapter 实现即可，工具层与引擎零改动。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from integrations.commerce.adapter import PlatformAdapter
from integrations.commerce.client import ChannelRestClient, RestApiError, get_rest_client
from integrations.commerce.models import (
    AfterSalesStatsResult,
    AnomaliesResult,
    InventoryResult,
    KnowledgeResult,
    OrderStatsResult,
    OrderStatusResult,
    PromotionRequest,
    PromotionResult,
    PromotionsResult,
    ServiceTicketResult,
    ShelfAction,
    ShelfResult,
    UpdatePriceResult,
)
from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY


def _current_policy():
    """动态取默认策略（允许 harness/测试在运行时替换为快配置）。"""
    from execution import policy as _ep

    return _ep.DEFAULT_EXECUTION_POLICY


async def invoke_operation(
    channel: Optional[str],
    operation: str,
    params: dict[str, Any],
    *,
    validate: bool = True,
) -> dict[str, Any]:
    """执行一次语义操作（策略管控下的共享执行通道）。

    channel 为 None 时为平台级操作（如知识库），使用默认客户端；
    渠道缺失/未启用由调用方提前判定（本层不吞配置错误）。

    幂等键由 ExecutionPolicy 依操作名判定：写操作自动生成
    idem_{session}_t{turn}_{tool}_{params_hash} 并在重试间复用。
    """
    adapter: PlatformAdapter = DEFAULT_CHANNEL_REGISTRY.executor_for(channel)
    client: ChannelRestClient | None = (
        DEFAULT_CHANNEL_REGISTRY.client_for(channel) if channel is not None else get_rest_client()
    )
    if client is None:
        raise RestApiError(10002, f"渠道不存在: {channel}")

    async def _run(idempotency_key: Optional[str]) -> dict[str, Any]:
        method, path, http_kwargs = adapter.build_request(operation, channel, params)
        data = await client.call(method, path, idempotency_key=idempotency_key, **http_kwargs)
        return adapter.parse_response(data)

    outcome = await _current_policy().execute(operation, _run, params=params, validate=validate)
    return outcome.value


def get_commerce_provider(channel: Optional[str] = None) -> "HttpCommerceProvider":
    """按渠道取 provider 实例（工具层入口）。"""
    return HttpCommerceProvider(channel)


class HttpCommerceProvider:
    """CommerceProvider 的 HTTP 适配实现（当前指向 mock 平台网关）。"""

    def __init__(self, channel: Optional[str] = None) -> None:
        self._channel = channel

    def _ch(self, channel: str) -> Optional[str]:
        return channel or self._channel

    async def query_inventory(self, sku_id: str, channel: str = "") -> InventoryResult:
        ch = self._ch(channel)
        data = await invoke_operation(ch, "query_inventory", {"sku": sku_id})
        return InventoryResult(
            sku_id=sku_id,
            name=str(data.get("name", "")),
            stock=int(data.get("stock", 0)),
            channel=ch or "",
            extra=data,
        )

    async def update_price(
        self,
        sku_id: str,
        price: Decimal,
        channel: str = "",
        cost_price: Decimal = Decimal("0"),
        idempotency_key: str | None = None,
    ) -> UpdatePriceResult:
        ch = self._ch(channel)
        data = await invoke_operation(
            ch,
            "update_price",
            {"sku": sku_id, "new_price": float(price), "cost_price": float(cost_price)},
        )
        return UpdatePriceResult(
            sku_id=sku_id,
            price=Decimal(str(data.get("new_price", price))),
            channel=ch or "",
            idempotent_replay=bool(data.get("idempotent_replay")),
            extra=data,
        )

    async def product_shelf(self, sku_id: str, action: ShelfAction | str, channel: str = "") -> ShelfResult:
        ch = self._ch(channel)
        act = action.value if isinstance(action, ShelfAction) else str(action)
        data = await invoke_operation(ch, "product_shelf", {"sku": sku_id, "action": act})
        return ShelfResult(
            sku_id=sku_id,
            action=ShelfAction(act),
            channel=ch or "",
            extra=data,
        )

    async def create_promotion(
        self,
        request: PromotionRequest,
        idempotency_key: str | None = None,
    ) -> PromotionResult:
        ch = self._ch(request.channel)
        data = await invoke_operation(
            ch,
            "create_promotion",
            {
                "sku": request.sku_id,
                "discount": request.discount,
                "start_time": request.start_time,
                "end_time": request.end_time,
            },
        )
        return PromotionResult(
            sku_id=request.sku_id,
            discount=float(data.get("discount", request.discount)),
            start_time=str(data.get("start_time", request.start_time)),
            end_time=str(data.get("end_time", request.end_time)),
            channel=ch or "",
            idempotent_replay=bool(data.get("idempotent_replay")),
            extra=data,
        )

    async def query_order_status(self, order_id: str, channel: str = "") -> OrderStatusResult:
        ch = self._ch(channel)
        data = await invoke_operation(ch, "query_order_status", {"order_id": order_id})
        return OrderStatusResult(
            order_id=order_id,
            status=str(data.get("status", "未知")),
            channel=ch or "",
            extra=data,
        )

    async def query_order_stats(self, channel: str = "", period: str = "近7天") -> OrderStatsResult:
        ch = self._ch(channel)
        data = await invoke_operation(ch, "query_order_stats", {"period": period})
        return OrderStatsResult(
            orders=int(data.get("orders", 0)),
            gmv=float(data.get("gmv", 0.0)),
            avg=float(data.get("avg", 0.0)),
            channel=ch or "",
            period=str(data.get("period", period)),
            extra=data,
        )

    async def create_service_ticket(
        self,
        order_id: str,
        issue: str,
        channel: str = "",
        priority: str = "normal",
        idempotency_key: str | None = None,
    ) -> ServiceTicketResult:
        ch = self._ch(channel)
        data = await invoke_operation(
            ch,
            "service_ticket",
            {"order_id": order_id, "issue": issue, "priority": priority},
        )
        return ServiceTicketResult(
            ticket_id=str(data.get("ticket_id", "")),
            order_id=order_id,
            channel=ch or "",
            idempotent_replay=bool(data.get("idempotent_replay")),
            extra=data,
        )

    async def query_anomalies(self, channel: str = "") -> AnomaliesResult:
        ch = self._ch(channel)
        data = await invoke_operation(ch, "query_anomalies", {})
        return AnomaliesResult(
            channel=ch or "",
            items=[str(i) for i in data.get("items", [])],
            extra=data,
        )

    async def query_promotions(self, channel: str = "") -> PromotionsResult:
        ch = self._ch(channel)
        data = await invoke_operation(ch, "query_promotions", {})
        return PromotionsResult(
            channel=ch or "",
            items=list(data.get("items", [])),
            extra=data,
        )

    async def query_after_sales_stats(
        self, channel: str = "", period: str = "近7天"
    ) -> AfterSalesStatsResult:
        ch = self._ch(channel)
        data = await invoke_operation(ch, "query_after_sales_stats", {"period": period})
        return AfterSalesStatsResult(
            channel=ch or "",
            refund_rate=float(data.get("refund_rate", 0.0)),
            tickets=int(data.get("tickets", 0)),
            period=str(data.get("period", period)),
            extra=data,
        )

    async def query_knowledge_base(self, topic: str) -> KnowledgeResult:
        data = await invoke_operation(None, "query_knowledge_base", {"topic": topic})
        return KnowledgeResult(
            topic=topic,
            matched=bool(data.get("matched")),
            text=data.get("text"),
            key=data.get("key"),
            extra=data,
        )
