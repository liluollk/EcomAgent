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

import asyncio
import hashlib
import hmac
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from decimal import Decimal, InvalidOperation

from integrations.commerce.client import ChannelRestClient, RestApiError, get_rest_client
from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceError,
    PriceErrorCode,
    PricePlatform,
    ProductSnapshot,
    PriceVerification,
    PriceWriteReceipt,
    ProductRef,
    as_price,
)

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


class _BasePriceAdapter(PlatformAdapter):
    """调价执行面 Adapter 基类：把 PricePlatform 三动作 + PlatformAdapter 接缝收敛。

    子类只需提供协议相关细节（端点、信封构造、价格单位、错误映射、响应解析）。
    共享的是单次请求通道、超时/连接失败的翻译，以及 idempotency_key 透传——
    这些与协议无关，收敛到一处避免两平台重复实现。
    """

    platform: str = ""
    price_scale: int = 2
    _endpoint: str = ""
    _method_snapshot: str = ""
    _method_update: str = ""
    _snapshot_response_key: str = ""
    _update_response_key: str = ""

    def __init__(self, client: "ChannelRestClient | None" = None) -> None:
        # 默认走离线 ASGI（mock_commerce.routes.app）；也可注入带短超时的 client 做故障测试
        self._client = client or get_rest_client()

    # --- PlatformAdapter 接缝（保留，供既有 channel 注册表/测试使用） ---
    def build_request(self, operation, channel, params):
        method = self._method_for(operation)
        return "POST", self._endpoint_for(operation), {"json_body": self._build_envelope(method, params)}

    async def probe(self, cfg):
        return {
            "ok": True,
            "message": f"{self.kind} 调价执行面就绪（离线契约实现，未经真实平台资质）",
            "data": {"platform": self.platform},
        }

    # --- PricePlatform 接缝（调价闭环依赖） ---
    async def query_snapshot(self, ref: ProductRef) -> "ProductSnapshot":
        env = self._build_envelope(self._method_snapshot, self._ref_params(ref))
        raw = await self._call_raw("POST", self._endpoint_for("snapshot"), env)
        data = self._unwrap(raw, self._snapshot_response_key)
        return self._parse_snapshot(ref, data)

    async def apply_price(self, command: PriceChangeCommand, *, idempotency_key=None) -> PriceWriteReceipt:
        ref = command.product_ref
        env = self._build_envelope(self._method_update, {
            **self._ref_params(ref),
            "price": self._price_out(command.target_price),
        })
        raw = await self._call_raw(
            "POST", self._endpoint_for("apply_price"), env, idempotency_key=idempotency_key, writing=True
        )
        data = self._unwrap(raw, self._update_response_key)
        return self._parse_write_receipt(ref, data)

    async def verify_price(self, ref: ProductRef, expected_price) -> PriceVerification:
        expected = as_price(expected_price)
        snapshot = await self.query_snapshot(ref)
        consistent = snapshot.current_price == expected
        return PriceVerification(
            product_ref=ref, expected_price=expected,
            observed_price=snapshot.current_price, consistent=consistent, attempts=1,
        )

    # --- 子类协议 ---
    def _method_for(self, operation):
        return self._method_snapshot if operation in ("snapshot", "verify") else self._method_update

    def _endpoint_for(self, operation):
        return self._endpoint

    def _ref_params(self, ref: ProductRef) -> dict:
        raise NotImplementedError

    def _build_envelope(self, method, params) -> dict:
        raise NotImplementedError

    def _price_out(self, price: Decimal):
        raise NotImplementedError

    def _price_in(self, raw) -> Decimal:
        raise NotImplementedError

    def _unwrap(self, raw, response_key) -> dict:
        raise NotImplementedError

    def _parse_snapshot(self, ref, data) -> "ProductSnapshot":
        raise NotImplementedError

    def _parse_write_receipt(self, ref, data) -> PriceWriteReceipt:
        raise NotImplementedError

    # --- 共享请求通道 ---
    async def _call_raw(self, method, path, envelope, *, idempotency_key=None, writing=False):
        # 在 adapter 边界强制「请求超时」：ASGI transport（离线测试）不会替我们触发
        # httpx 的读超时，这里用 asyncio.wait_for 统一把超时翻译成 PriceError，
        # 写超时必须标记 side_effect_possible=True（服务端可能已生效，必须回查而非重试写）。
        transport = getattr(self._client, "_client", None)
        read_timeout = getattr(getattr(transport, "timeout", None), "read", None)
        coro = self._client.call_raw(
            method, path, json_body=envelope, idempotency_key=idempotency_key
        )
        try:
            raw = await asyncio.wait_for(coro, timeout=read_timeout) if read_timeout else await coro
        except asyncio.TimeoutError as e:
            raise PriceError(
                PriceErrorCode.TRANSIENT_ERROR, f"{self.platform} 请求超时",
                platform=self.platform, side_effect_possible=writing,
            ) from e
        except PriceError:
            raise
        except httpx.TimeoutException as e:
            # 真实 HTTP transport 下由 httpx 直接抛出的超时
            raise PriceError(
                PriceErrorCode.TRANSIENT_ERROR, f"{self.platform} 请求超时",
                platform=self.platform, side_effect_possible=writing,
            ) from e
        except httpx.HTTPStatusError as e:
            raise PriceError(
                self._map_status(e.response.status_code),
                f"{self.platform} HTTP {e.response.status_code}", platform=self.platform,
            ) from e
        except RestApiError as e:
            raise PriceError(
                PriceErrorCode.FATAL_ERROR, f"{self.platform} 响应无法解析: {e}", platform=self.platform
            ) from e
        except httpx.HTTPError as e:
            raise PriceError(
                PriceErrorCode.TRANSIENT_ERROR, f"{self.platform} 连接失败: {e}", platform=self.platform
            ) from e
        return raw

    @staticmethod
    def _map_status(status: int) -> PriceErrorCode:
        if status == 429:
            return PriceErrorCode.TRANSIENT_ERROR
        if status >= 500:
            return PriceErrorCode.FATAL_ERROR
        return PriceErrorCode.CLIENT_ERROR


class TaobaoAdapter(_BasePriceAdapter):
    """淘宝开放平台 TOP 风格调价 Adapter（离线契约实现）。

    单端点 POST /taobao/top/api，按信封 method 分发；参数信封 + HMAC-SHA256 签名；
    价格以「元·两位小数字符串」收发。鉴权（app_key/session/sign）只在 Adapter 内生成。
    """

    kind = "taobao"
    platform = "taobao"
    price_scale = 2
    _endpoint = "/taobao/top/api"
    _method_snapshot = "taobao.item.sku.get"
    _method_update = "taobao.item.sku.price.update"
    _snapshot_response_key = "item_sku_get_response"
    _update_response_key = "item_sku_price_update_response"
    _APP_KEY = "mock_app_key"
    _APP_SECRET = "mock_app_secret"
    _SESSION = "mock_session"

    def _ref_params(self, ref: ProductRef) -> dict:
        return {"num_iid": ref.product_id, "sku_id": ref.sku_id, "shop_id": ref.shop_id}

    def _build_envelope(self, method, params) -> dict:
        base = {
            "method": method,
            "app_key": self._APP_KEY,
            "session": self._SESSION,
            "timestamp": str(int(time.time())),
            "format": "json",
            "v": "2.0",
            "sign_method": "hmac-sha256",
        }
        all_params = {**base, **params}
        base["sign"] = self._sign({k: v for k, v in all_params.items() if k != "sign"})
        return {**base, **params}

    def _sign(self, params) -> str:
        items = sorted((str(k), str(v)) for k, v in params.items())
        raw = self._APP_SECRET + "".join(f"{k}{v}" for k, v in items) + self._APP_SECRET
        return hmac.new(self._APP_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest().upper()

    def _price_out(self, price: Decimal) -> str:
        return f"{Decimal(price).quantize(Decimal('0.01'))}"

    def _price_in(self, raw) -> Decimal:
        return Decimal(str(raw))

    def _unwrap(self, raw, response_key) -> dict:
        if not isinstance(raw, dict):
            raise PriceError(PriceErrorCode.FATAL_ERROR, "淘宝返回结构异常", platform="taobao")
        if "error_response" in raw:
            err = raw["error_response"]
            code = err.get("code")
            raise PriceError(self._map_code(code), str(err.get("msg", "淘宝错误")), platform="taobao", platform_code=code)
        resp = raw.get(response_key) or {}
        if resp.get("code", 0) not in (0, None):
            code = resp.get("code")
            raise PriceError(self._map_code(code), str(resp.get("msg", "淘宝错误")), platform="taobao", platform_code=code)
        data = resp.get("data") or {}
        if "price" not in data:
            raise PriceError(PriceErrorCode.FATAL_ERROR, "淘宝响应缺少 price 字段", platform="taobao")
        # 畸形成功响应（信封完好但价格字段不可解析）→ 统一转为 PriceError
        try:
            self._price_in(data["price"])
        except (InvalidOperation, ValueError, TypeError):
            raise PriceError(PriceErrorCode.FATAL_ERROR, "淘宝返回价格无法解析", platform="taobao")
        return data

    def _parse_snapshot(self, ref, data) -> "ProductSnapshot":
        return ProductSnapshot(
            product_ref=ref, name="",
            current_price=self._price_in(data.get("price", "0")),
            stock=int(data.get("num", 0)),
            status=str(data.get("status", "")),
            activity_name=str(data.get("activity", "")),
            activity_locked=bool(data.get("activity_locked", False)),
            observed_at="",
        )

    def _parse_write_receipt(self, ref, data) -> PriceWriteReceipt:
        return PriceWriteReceipt(
            product_ref=ref,
            applied_price=self._price_in(data.get("price", "0")),
            idempotent_replay=bool(data.get("idempotent_replay", False)),
            attempts=1,
        )

    @staticmethod
    def _map_code(code) -> PriceErrorCode:
        return {
            15: PriceErrorCode.CLIENT_ERROR,    # 参数非法
            21: PriceErrorCode.CLIENT_ERROR,    # 鉴权失败
            26: PriceErrorCode.CLIENT_ERROR,    # 权限不足
            27: PriceErrorCode.CLIENT_ERROR,    # 商品不存在
            40: PriceErrorCode.BUSINESS_ERROR,  # 价格低于成本/合规拦截
            41: PriceErrorCode.BUSINESS_ERROR,  # 活动锁价
            7: PriceErrorCode.TRANSIENT_ERROR,  # 限流
            8: PriceErrorCode.TRANSIENT_ERROR,  # 系统繁忙
            9998: PriceErrorCode.CAPABILITY_UNSUPPORTED,  # 能力不支持
        }.get(code, PriceErrorCode.FATAL_ERROR)


class DouyinAdapter(_BasePriceAdapter):
    """抖音电商开放平台调价 Adapter（离线契约实现）。

    JSON 请求体 + access_token；价格以「分·整数」收发；成功 {code,msg,data}、
    错误 {code,msg}（code!=0）。与淘宝同打到一份离线 mock 网关，但协议形态完全不同。
    鉴权（access_token/app_key/sign）只在 Adapter 内生成。
    """

    kind = "douyin"
    platform = "douyin"
    price_scale = 0
    _method_snapshot = ""
    _method_update = ""
    _snapshot_response_key = ""
    _update_response_key = ""
    _ENDPOINT_SNAPSHOT = "/douyin/product/sku/get"
    _ENDPOINT_UPDATE = "/douyin/product/sku/price"
    _APP_KEY = "mock_app_key"
    _APP_SECRET = "mock_app_secret"
    _ACCESS_TOKEN = "mock_access_token"

    def _endpoint_for(self, operation):
        return self._ENDPOINT_SNAPSHOT if operation in ("snapshot", "verify") else self._ENDPOINT_UPDATE

    def _ref_params(self, ref: ProductRef) -> dict:
        return {"product_id": ref.product_id, "sku_id": ref.sku_id, "shop_id": ref.shop_id}

    def _build_envelope(self, method, params) -> dict:
        base = {
            "access_token": self._ACCESS_TOKEN,
            "app_key": self._APP_KEY,
            "sign": self._sign(params),
            "timestamp": int(time.time()),
        }
        return {**base, **params}

    def _sign(self, params) -> str:
        items = sorted((str(k), str(v)) for k, v in params.items())
        raw = self._APP_SECRET + "".join(f"{k}{v}" for k, v in items) + self._APP_SECRET
        return hmac.new(self._APP_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest().upper()

    def _price_out(self, price: Decimal) -> int:
        return int((Decimal(price) * 100).to_integral_value())

    def _price_in(self, raw) -> Decimal:
        return Decimal(int(raw)) / 100

    def _unwrap(self, raw, response_key) -> dict:
        if not isinstance(raw, dict):
            raise PriceError(PriceErrorCode.FATAL_ERROR, "抖店返回结构异常", platform="douyin")
        code = raw.get("code", 0)
        if code != 0:
            raise PriceError(self._map_code(code), str(raw.get("msg", "抖店错误")), platform="douyin", platform_code=code)
        data = raw.get("data") or {}
        if "price" not in data:
            raise PriceError(PriceErrorCode.FATAL_ERROR, "抖店响应缺少 price 字段", platform="douyin")
        try:
            self._price_in(data["price"])
        except (InvalidOperation, ValueError, TypeError):
            raise PriceError(PriceErrorCode.FATAL_ERROR, "抖店返回价格无法解析", platform="douyin")
        return data

    def _parse_snapshot(self, ref, data) -> "ProductSnapshot":
        return ProductSnapshot(
            product_ref=ref, name="",
            current_price=self._price_in(data.get("price", 0)),
            stock=int(data.get("stock", 0)),
            status=str(data.get("status", "")),
            activity_name=str(data.get("promotion", "")),
            activity_locked=bool(data.get("promotion_locked", False)),
            observed_at="",
        )

    def _parse_write_receipt(self, ref, data) -> PriceWriteReceipt:
        return PriceWriteReceipt(
            product_ref=ref,
            applied_price=self._price_in(data.get("price", 0)),
            idempotent_replay=bool(data.get("idempotent_replay", False)),
            attempts=1,
        )

    @staticmethod
    def _map_code(code) -> PriceErrorCode:
        return {
            40001: PriceErrorCode.CLIENT_ERROR,   # 参数错误
            40002: PriceErrorCode.CLIENT_ERROR,   # 鉴权失败
            40010: PriceErrorCode.CLIENT_ERROR,   # 商品不存在
            30001: PriceErrorCode.BUSINESS_ERROR,  # 价格不合规
            30002: PriceErrorCode.BUSINESS_ERROR,  # 活动锁价
            20001: PriceErrorCode.TRANSIENT_ERROR,  # 系统繁忙/限流
            50000: PriceErrorCode.FATAL_ERROR,     # 系统错误
            90000: PriceErrorCode.CAPABILITY_UNSUPPORTED,  # 能力不支持
        }.get(code, PriceErrorCode.FATAL_ERROR)


class _StubAdapter(PlatformAdapter):
    """真实平台（京东 / 自定义开放平台）adapter 的 stub：诚实声明尚未接入。"""

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


class JdAdapter(_StubAdapter):
    kind = "jd"


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