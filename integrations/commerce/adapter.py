"""
平台适配器抽象层 — 在工具层与 Commerce Client 之间提供可插拔接缝。

设计：
  - PlatformAdapter：抽象接缝。工具层只调 `_exec(channel, operation, params)`，
    由 adapter 负责 build_request（出 method/path/http_kwargs）与
    parse_response（归一到 mock 同款字段名，保证工具文本模板逐字不变）。
  - MockAdapter：把原本散在 11 个工具里的 (method, path, body) 映射收拢到这里，
    是「第一个 adapter」，也是默认行为（platform=mock 时字节级不变）。
  - Taobao / Jd / Douyin / GenericOpen adapter：**stub 占位**。真实签名与字段
    映射需平台资质后在各 adapter 里补，此处只留接口与诚实提示。

契约（关键）：parse_response 返回的 dict，字段名必须与 mock 平台返回一致
（stock/name/status/orders/items/refund_rate/ticket_id/idempotent_replay 等），
这样工具层的文本模板完全不用改。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Optional

PLATFORM_KINDS = ["mock", "taobao", "jd", "douyin", "open"]

PLATFORM_LABELS = {
    "mock": "本地 Mock（免费离线）",
    "taobao": "淘宝开放平台 TOP",
    "jd": "京东宙斯 JOS",
    "douyin": "抖音电商开放平台",
    "open": "自定义开放平台（generic）",
}


class PlatformAdapter(ABC):
    """平台适配器接缝。

    每个平台（或 mock）实现同一套接口，工具层 / 测连通端点只依赖本契约。
    """

    kind: str

    @abstractmethod
    def build_request(
        self,
        operation: str,
        channel: Optional[str],
        params: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        """把「语义操作名 + 业务参数」翻译成一次平台 HTTP 请求。"""

    def parse_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """把平台返回归一化成 mock 同款字段名的 dict。"""
        return data

    async def probe(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """连通性探测。"""
        raise NotImplementedError(f"{self.kind} adapter 未实现 probe")


class MockAdapter(PlatformAdapter):
    """Mock 平台适配器 — 承载原本散在 11 个工具里的 (method, path, body) 映射。"""

    kind = "mock"

    _OPERATIONS: dict[str, tuple[str, str, str]] = {
        "query_inventory": ("GET", "/v1/{channel}/inventory", "params"),
        "update_price": ("PUT", "/v1/{channel}/price", "json_body"),
        "create_promotion": ("POST", "/v1/{channel}/promotions", "json_body"),
        "query_order_status": ("GET", "/v1/{channel}/orders/{order_id}", "params"),
        "product_shelf": ("PUT", "/v1/{channel}/shelf", "json_body"),
        "service_ticket": ("POST", "/v1/{channel}/service-tickets", "json_body"),
        "query_order_stats": ("GET", "/v1/{channel}/order-stats", "params"),
        "query_anomalies": ("GET", "/v1/{channel}/anomalies", "params"),
        "query_promotions": ("GET", "/v1/{channel}/promotions", "params"),
        "query_after_sales_stats": ("GET", "/v1/{channel}/after-sales-stats", "params"),
        "query_knowledge_base": ("GET", "/v1/knowledge-base", "params"),
    }

    def build_request(self, operation, channel, params):
        spec = self._OPERATIONS.get(operation)
        if spec is None:
            raise ValueError(f"未知操作: {operation}（mock 适配器仅支持 {sorted(self._OPERATIONS)}）")
        method, template, kind = spec
        if "{channel}" in template and channel is None:
            raise ValueError(f"操作 {operation} 需要渠道上下文，但 channel 为 None")
        path = template.replace("{channel}", channel or "")
        path_params = re.findall(r"\{([^}]+)\}", path)
        remaining = dict(params)
        for key in path_params:
            if key in remaining:
                path = path.replace("{" + key + "}", str(remaining.pop(key)))
        if kind == "params":
            kwargs = {"params": remaining}
        else:
            kwargs = {"json_body": remaining}
        return method, path, kwargs

    async def probe(self, cfg):
        return {
            "ok": True,
            "message": "Mock 平台就绪（离线直连，无需网络）",
            "data": {"platform": "mock"},
        }


class _StubAdapter(PlatformAdapter):
    """真实平台 adapter 的 stub 基类。"""

    def build_request(self, operation, channel, params):
        raise NotImplementedError(
            f"平台 {self.kind} 尚未接入（需平台资质 + base_url + 签名/字段映射实现），"
            f"无法执行操作 {operation}"
        )

    async def probe(self, cfg):
        return {
            "ok": False,
            "message": f"平台 {self.kind} 尚未接入：需平台资质与 base_url/签名配置后再测连通",
            "data": {"platform": self.kind},
        }

    def __repr__(self):
        return f"<{type(self).__name__} kind={self.kind} (stub)>"


class TaobaoAdapter(_StubAdapter):
    kind = "taobao"


class JdAdapter(_StubAdapter):
    kind = "jd"


class DouyinAdapter(_StubAdapter):
    kind = "douyin"


class GenericOpenAdapter(_StubAdapter):
    kind = "open"


ADAPTER_REGISTRY: dict[str, type[PlatformAdapter]] = {
    "mock": MockAdapter,
    "taobao": TaobaoAdapter,
    "jd": JdAdapter,
    "douyin": DouyinAdapter,
    "open": GenericOpenAdapter,
}


def get_adapter(platform: str) -> PlatformAdapter:
    cls = ADAPTER_REGISTRY.get(platform or "mock")
    if cls is None:
        cls = MockAdapter
    return cls()


def platform_is_real(platform: str) -> bool:
    platform = platform or "mock"
    return platform != "mock" and platform in PLATFORM_KINDS


__all__ = [
    "PlatformAdapter",
    "MockAdapter",
    "TaobaoAdapter",
    "JdAdapter",
    "DouyinAdapter",
    "GenericOpenAdapter",
    "PLATFORM_KINDS",
    "PLATFORM_LABELS",
    "ADAPTER_REGISTRY",
    "get_adapter",
    "platform_is_real",
]