"""平台能力画像 — 声明每个平台「能做什么」以及「价格以什么精度表达」。

为什么需要它：调价闭环里的每一步（查快照 / 改价 / 回查）都不是所有平台都
提供的。缺失能力必须在进入执行前被判定为 CAPABILITY_UNSUPPORTED，而不是
等到请求打到平台上再失败，也不是静默降级成「跳过回查」——跳过回查等于
把「写接口返回 200」当成「价格已生效」。

同时它承载价格精度约束：目标价在领域层统一用 Decimal 表达，
各平台用自己的精度（淘宝元/两位小数、抖店分/整数），换算在 Adapter 里做。

mock 只是离线测试平台：它共享淘宝/抖店两套协议形态，用于在没有平台资质
的情况下验证契约，不代表真实生产接入。
"""

from __future__ import annotations

from dataclasses import dataclass

from integrations.commerce.price_models import PriceError, PriceErrorCode

CAPABILITY_NAMES = ("query_snapshot", "update_price", "verify_price")


@dataclass(frozen=True)
class PlatformCapabilities:
    """单个平台的能力画像（不可变，避免运行中被就地改写）。"""

    platform: str
    query_snapshot: bool
    update_price: bool
    verify_price: bool
    price_scale: int
    requires_signature: bool
    label: str = ""

    def supports(self, capability: str) -> bool:
        """是否支持某个能力；未知能力名返回 False。"""
        if capability not in CAPABILITY_NAMES:
            return False
        return bool(getattr(self, capability))


PLATFORM_CAPABILITIES: dict[str, PlatformCapabilities] = {
    # 淘宝：价格以「元」表达，两位小数；TOP 风格参数信封 + 签名
    "taobao": PlatformCapabilities(
        platform="taobao",
        query_snapshot=True,
        update_price=True,
        verify_price=True,
        price_scale=2,
        requires_signature=True,
        label="淘宝开放平台 TOP",
    ),
    # 抖店：价格字段以「分」表达，整数；JSON 请求体 + access token + 签名
    "douyin": PlatformCapabilities(
        platform="douyin",
        query_snapshot=True,
        update_price=True,
        verify_price=True,
        price_scale=0,
        requires_signature=True,
        label="抖音电商开放平台",
    ),
    # 离线测试平台：无签名要求，同时提供两套协议形态供契约验证
    "mock": PlatformCapabilities(
        platform="mock",
        query_snapshot=True,
        update_price=True,
        verify_price=True,
        price_scale=2,
        requires_signature=False,
        label="本地 Mock（离线测试平台，可模拟淘宝/抖店协议形态）",
    ),
}


def register_capabilities(capabilities: PlatformCapabilities) -> None:
    """注册/覆盖一个平台的能力画像（新增平台接入时调用）。"""
    PLATFORM_CAPABILITIES[capabilities.platform] = capabilities


def get_capabilities(platform: str) -> PlatformCapabilities:
    """取平台能力画像；平台缺失或未登记 → CAPABILITY_UNSUPPORTED。"""
    name = (platform or "").strip().lower()
    if not name:
        raise PriceError(
            PriceErrorCode.CAPABILITY_UNSUPPORTED,
            "未指定平台：调价必须显式声明平台（taobao / douyin）",
        )
    capabilities = PLATFORM_CAPABILITIES.get(name)
    if capabilities is None:
        raise PriceError(
            PriceErrorCode.CAPABILITY_UNSUPPORTED,
            f"平台 {name} 未登记能力画像（已登记: {sorted(PLATFORM_CAPABILITIES)}）",
            platform=name,
        )
    return capabilities


def require_capability(platform: str, capability: str) -> PlatformCapabilities:
    """断言平台具备某能力；缺失即返回 CAPABILITY_UNSUPPORTED。

    Returns:
        PlatformCapabilities: 平台能力画像（调用方可继续读取 price_scale 等约束）。
    """
    capabilities = get_capabilities(platform)
    if not capabilities.supports(capability):
        raise PriceError(
            PriceErrorCode.CAPABILITY_UNSUPPORTED,
            f"平台 {capabilities.platform} 不支持 {capability}",
            platform=capabilities.platform,
        )
    return capabilities


__all__ = [
    "CAPABILITY_NAMES",
    "PLATFORM_CAPABILITIES",
    "PlatformCapabilities",
    "get_capabilities",
    "register_capabilities",
    "require_capability",
]
