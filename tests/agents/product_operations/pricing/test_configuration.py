"""价格策略发布载荷的规范化/哈希/严格还原(P1 Task 10;设计 §5.2、§6)。

这三件事是"幂等发布 + 装配失败闭合"的底座:

- 规范化必须与输入对象无关地稳定,否则同配置每次装配都会被误判成"配置变了";
- 哈希必须对键顺序不敏感、对值敏感,否则脏数据能伪造合法 hash;
- 严格还原必须拒绝缺字段、多字段与非法值域,否则脏载荷会带着合法 hash 进入执行。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agents.product_operations.policy.models import PricePolicyConfig
from agents.product_operations.pricing.configuration import (
    PRICING_POLICY_CONFIG_INVALID,
    InvalidPricingPolicyConfig,
    canonical_pricing_policy_payload,
    parse_pricing_policy_config,
    pricing_policy_config_hash,
)


def _config(**overrides: object) -> PricePolicyConfig:
    base: dict[str, object] = {
        "policy_version": "price-policy-v1",
        "currency": "CNY",
        "min_allowed_price": Decimal("50.00"),
        "max_price_change": Decimal("100.00"),
    }
    base.update(overrides)
    return PricePolicyConfig(**base)  # type: ignore[arg-type]


def test_canonical_payload_is_stable_and_json_safe() -> None:
    """载荷键集合固定、Decimal 转尺度无关字符串,同一配置产生逐字节相同载荷。"""
    payload = canonical_pricing_policy_payload(_config())

    assert payload == {
        "policy_version": "price-policy-v1",
        "currency": "CNY",
        "min_allowed_price": "50",
        "max_price_change": "100",
    }
    assert all(isinstance(value, str) for value in payload.values())
    # 规范化消除尾随零:`Decimal("50.00") == Decimal("50.000")` 必须同载荷,
    # 否则"数值相同的配置"会算出不同 hash,幂等发布失效。
    assert canonical_pricing_policy_payload(
        _config(min_allowed_price=Decimal("50.000"), max_price_change=Decimal("1E+2"))
    ) == canonical_pricing_policy_payload(_config())


def test_canonical_payload_avoids_scientific_notation() -> None:
    """整数价不落成 ``5E+1``:科学计数法会污染载荷可读性与下游解析。"""
    payload = canonical_pricing_policy_payload(
        _config(min_allowed_price=Decimal("50.00"))
    )

    assert payload["min_allowed_price"] == "50"
    assert "E" not in payload["min_allowed_price"].upper()


def test_hash_is_key_order_insensitive_but_value_sensitive() -> None:
    """哈希只看内容:键顺序不影响;任一值变化都换 hash。"""
    canonical = canonical_pricing_policy_payload(_config())
    reordered = dict(reversed(list(canonical.items())))

    assert pricing_policy_config_hash(canonical) == pricing_policy_config_hash(reordered)

    changed = canonical_pricing_policy_payload(_config(max_price_change=Decimal("80.00")))
    assert pricing_policy_config_hash(canonical) != pricing_policy_config_hash(changed)


def test_hash_matches_the_published_version_hash_for_same_config() -> None:
    """相同配置 → 相同 hash(幂等发布判据);不同配置 → 不同 hash(递增版本判据)。"""
    first = pricing_policy_config_hash(canonical_pricing_policy_payload(_config()))
    again = pricing_policy_config_hash(canonical_pricing_policy_payload(_config()))
    other = pricing_policy_config_hash(
        canonical_pricing_policy_payload(_config(policy_version="price-policy-v2"))
    )

    assert first == again
    assert first != other


def test_parse_round_trips_a_canonical_payload() -> None:
    """规范化载荷能被严格还原,且与原配置等价(发布-读取闭环)。"""
    config = _config()
    payload = canonical_pricing_policy_payload(config)

    assert parse_pricing_policy_config(payload) == config


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"policy_version": "v1", "currency": "CNY"}, id="missing-fields"),
        pytest.param(
            {
                "policy_version": "v1",
                "currency": "CNY",
                "min_allowed_price": "50.00",
                "max_price_change": "100.00",
                "unexpected": "field",
            },
            id="extra-field",
        ),
        pytest.param(
            {
                "policy_version": "v1",
                "currency": "CNY",
                "min_allowed_price": "not-a-decimal",
                "max_price_change": "100.00",
            },
            id="illegal-value",
        ),
    ],
)
def test_parse_rejects_payloads_that_are_not_a_config(payload: dict[str, str]) -> None:
    """缺字段、多字段、非法值域都必须以规范错误码 fail closed。"""
    with pytest.raises(InvalidPricingPolicyConfig) as raised:
        parse_pricing_policy_config(payload)

    assert raised.value.reason_code == PRICING_POLICY_CONFIG_INVALID
    # 诊断不得回显原始载荷(可能来自数据库脏数据)。
    assert "unexpected" not in str(raised.value)
    assert "not-a-decimal" not in str(raised.value)
