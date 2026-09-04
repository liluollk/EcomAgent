"""
平台适配器抽象层 — 在 MCP 工具层与 ChannelRestClient 之间提供可插拔接缝。

为什么需要：
  - 现在每个工具在 body 里硬编码 (method, "/v1/{channel}/xxx", body)，且直接读
    mock 特有字段。真实平台（淘宝 TOP / 京东 JOS / 抖音开放平台）的网关地址、
    鉴权签名、请求参数装配、响应/错误结构全不相同。
  - 本层把「语义操作名 → 平台请求」与「平台响应 → 归一化字段」收进 adapter，
    让工具层不再感知 path / 字段 / 签名差异。

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

# 可选的平台类型（渠道配置的 platform 字段取值）
PLATFORM_KINDS = ["mock", "taobao", "jd", "douyin", "open"]

# 平台展示名（前端下拉用）
PLATFORM_LABELS = {
    "mock": "本地 Mock（免费离线）",
    "taobao": "淘宝开放平台 TOP",
    "jd": "京东宙斯 JOS",
    "douyin": "抖音电商开放平台",
    "open": "自定义开放平台（generic）",
}


class PlatformAdapter(ABC):
    """平台适配器接缝。

    每个平台（或 mock）实现同一套接口，MCP 工具层 / 测连通端点只依赖本契约。
    """

    #: 对应渠道配置的 platform 字段值
    kind: str

    @abstractmethod
    def build_request(
        self,
        operation: str,
        channel: Optional[str],
        params: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        """把「语义操作名 + 业务参数」翻译成一次平台 HTTP 请求。

        Args:
            operation: 语义操作名（与工具名一致，如 "query_inventory"）。
            channel: 渠道名（平台级操作传 None）。
            params: 业务参数字典（工具入参）。

        Returns:
            (method, path, http_kwargs)：method=GET/POST/PUT；path 为相对 base_url
            的路径或网关端点；http_kwargs 为传给 ChannelRestClient.call 的关键字
            （如 params / json_body）。
        """

    def parse_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """把平台返回归一化成 mock 同款字段名的 dict。

        默认实现 = 恒等（mock 的 call() 已解包到 {code,data} 的 data，字段即归一）。
        真实平台在各自 adapter 覆写：解包 TOP/JOS/抖店的响应信封、重映射字段成
        mock 同款，并将平台错误码抬平为 RestApiError。
        """
        return data

    async def probe(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """连通性探测：adapter 自己的可用性检查（用于设置页「测连通」）。

        Returns:
            {"ok": bool, "message": str, "data"?: Any}
        """
        raise NotImplementedError(f"{self.kind} adapter 未实现 probe")


class MockAdapter(PlatformAdapter):
    """Mock 平台适配器 — 承载原本散在 11 个工具里的 (method, path, body) 映射。"""

    kind = "mock"

    # 语义操作名 → (HTTP method, path 模板, 请求体字段装箱方式)
    # "{channel}" 会被替换为渠道名；channel=None 的平台级操作模板不含 {channel}。
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
        # 路径参数替换：模板里的 {order_id} 等从 params 取值内联，并从请求体移除
        path_params = re.findall(r"\{([^}]+)\}", path)
        remaining = dict(params)
        for key in path_params:
            if key in remaining:
                path = path.replace("{" + key + "}", str(remaining.pop(key)))
        if kind == "params":
            kwargs = {"params": remaining}
        else:  # json_body
            kwargs = {"json_body": remaining}
        return method, path, kwargs

    async def probe(self, cfg):
        """mock 平台连通性：默认在线（离线直连或 CHANNEL_API_URL），这里不做真实网络。"""
        return {
            "ok": True,
            "message": "Mock 平台就绪（离线直连，无需网络）",
            "data": {"platform": "mock"},
        }


class _StubAdapter(PlatformAdapter):
    """真实平台 adapter 的 stub 基类 — 占位接缝，等平台资质后进行真实实现。

    子类只需设置 kind 与一个「尚未接入」提示，即可让链路完整走通
    （build_request 抛不可执行、probe 诚实说明未接入），不误报连通。
    """

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
    """淘宝开放平台 TOP adapter（stub）。真实实现：app_key+timestamp+sign(MD5/HMAC)。"""

    kind = "taobao"


class JdAdapter(_StubAdapter):
    """京东宙斯 JOS adapter（stub）。真实实现：OAuth token + HMAC_MD5 签名。"""

    kind = "jd"


class DouyinAdapter(_StubAdapter):
    """抖音电商开放平台 adapter（stub）。真实实现：OAuth2 token + 双签名规范。"""

    kind = "douyin"


class GenericOpenAdapter(_StubAdapter):
    """自定义开放平台 adapter（stub）。真实实现：通用 base_url + 简单签名 + 字段映射。"""

    kind = "open"


# 平台 kind → adapter 类
ADAPTER_REGISTRY: dict[str, type[PlatformAdapter]] = {
    "mock": MockAdapter,
    "taobao": TaobaoAdapter,
    "jd": JdAdapter,
    "douyin": DouyinAdapter,
    "open": GenericOpenAdapter,
}


def get_adapter(platform: str) -> PlatformAdapter:
    """按 platform 字段拿 adapter 实例；未知平台回退 mock（保持默认行为）。"""
    cls = ADAPTER_REGISTRY.get(platform or "mock")
    if cls is None:
        cls = MockAdapter
    return cls()


def platform_is_real(platform: str) -> bool:
    """是否真实平台（非 mock）。真实平台不能走离线 ASGI 兜底。"""
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