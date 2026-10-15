"""Pricing 策略的发布载荷与已发布快照值对象(P1 Task 10;设计 §5.2、§6)。

价格规则必须来自**已发布的 Workspace 配置**,不能来自 Prompt、环境变量或模块
常量。同一份配置既要驱动 PreToolUse 的业务预检,也要驱动执行前校验,还要进入
Run 的策略快照与确认指纹——否则"用户确认的到底是哪一版规则"无法解释。

本模块只做三件与存储无关的事:

- 把 :class:`PricePolicyConfig` 规范化成可哈希的发布载荷(``Decimal`` → 字符串、
  键集合固定),让"相同配置幂等复用、配置变化才递增版本"有确定判据;
- 计算载荷的 ``config_hash``;
- 把已发布载荷**严格**还原成 :class:`PricePolicyConfig`,形状不符即
  ``PRICING_POLICY_CONFIG_INVALID``。

数据库读写留在组合根
(:mod:`composition.provisioning.product_operations`),因此本模块不 import 任何
runtime 持久化模型,``agents`` 也不知道 ``business_policy_versions`` 的存在。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from agents.product_operations.policy.models import PricePolicyConfig


PRICING_POLICY_STABLE_KEY: Final = "product-pricing"
"""价格策略在 ``business_policies`` 中的 stable key(P1 只有一个价格策略)。"""

PRICING_POLICY_NOT_PUBLISHED: Final = "PRICING_POLICY_NOT_PUBLISHED"
"""策略缺失、跨 Workspace、非 ``PUBLISHED`` 或 hash 不符时的装配错误码。"""

PRICING_POLICY_CONFIG_INVALID: Final = "PRICING_POLICY_CONFIG_INVALID"
"""已发布载荷无法还原成 :class:`PricePolicyConfig` 时的装配错误码。"""


@dataclass(frozen=True)
class PublishedPricingPolicy:
    """已发布的价格策略:**精确行** + 已校验的配置。

    ``version`` 是 ``BusinessPolicyVersion.version``——确认指纹绑定用的策略修订号;
    ``version_id`` 是它的主键——Run 策略快照记录"这次运行读的是哪一行"。两者不可
    互相替代:修订号用于跨提交比较,主键用于事后精确追溯。
    """

    version_id: uuid.UUID
    version: int
    config: PricePolicyConfig
    config_hash: str


class InvalidPricingPolicyConfig(ValueError):
    """已发布载荷的形状非法(缺字段、多字段或值域不符)。

    异常只携带规范错误码与安全诊断,不携带原始载荷——载荷可能来自数据库脏数据,
    不应原样进入日志或模型上下文。
    """

    reason_code: Final = PRICING_POLICY_CONFIG_INVALID

    def __init__(self, detail: str) -> None:
        super().__init__(f"{PRICING_POLICY_CONFIG_INVALID}: {detail}")
        self.detail = detail


def _canonical_decimal_text(value: Decimal) -> str:
    """把 ``Decimal`` 渲染成**尺度无关**的字符串。

    ``Decimal("50.00") == Decimal("50.000")`` 为真,但 ``str()`` 不同;若直接落库,
    "数值相同的配置"会算出不同 hash,幂等发布随之失效(每次都误判成配置变了)。
    这里统一成最短精确表示:先 ``normalize()`` 去尾随零,再以定点格式渲染,避免
    ``5E+1`` 这类科学计数法进入载荷。
    """
    return format(value.normalize(), "f")


def canonical_pricing_policy_payload(config: PricePolicyConfig) -> dict[str, str]:
    """把配置规范化成发布载荷:键集合固定,``Decimal`` 一律转尺度无关字符串。

    规范化是"幂等发布"的前提:同一份配置必须产生**逐字节相同**的载荷,否则
    hash 会漂移,每次装配都会误判成"配置变了"而递增版本。
    """
    return {
        "policy_version": config.policy_version,
        "currency": config.currency,
        "min_allowed_price": _canonical_decimal_text(config.min_allowed_price),
        "max_price_change": _canonical_decimal_text(config.max_price_change),
    }


def pricing_policy_config_hash(payload: Mapping[str, Any]) -> str:
    """载荷的 canonical JSON SHA-256(发布判据与读取校验共用同一算法)。"""
    canonical = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_pricing_policy_config(payload: Mapping[str, Any]) -> PricePolicyConfig:
    """把已发布载荷严格还原成配置;形状任何不符都抛 :class:`InvalidPricingPolicyConfig`。

    ``PricePolicyConfig`` 是 ``extra="forbid"`` 的冻结模型,因此这一步同时覆盖
    "字段齐全""无多余字段""值域合法"三项校验,不需要另写一遍手写检查。
    """
    try:
        return PricePolicyConfig.model_validate(dict(payload))
    except ValidationError as error:
        raise InvalidPricingPolicyConfig(
            f"published payload does not match PricePolicyConfig: {error.error_count()} error(s)"
        ) from error


__all__ = [
    "PRICING_POLICY_CONFIG_INVALID",
    "PRICING_POLICY_NOT_PUBLISHED",
    "PRICING_POLICY_STABLE_KEY",
    "InvalidPricingPolicyConfig",
    "PublishedPricingPolicy",
    "canonical_pricing_policy_payload",
    "parse_pricing_policy_config",
    "pricing_policy_config_hash",
]
