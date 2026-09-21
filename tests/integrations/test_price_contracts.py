"""调价领域契约测试 — 五个领域对象、Decimal 目标价、平台私有字段隔离、能力画像。

这组测试固定「跨平台调价语言」的形状：领域层只认这些类型，
任何平台私有字段（签名 / access token / 成本价）都不允许进入领域命令。
"""

from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from integrations.commerce.capabilities import (
    PLATFORM_CAPABILITIES,
    PlatformCapabilities,
    get_capabilities,
    require_capability,
)
from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceConstraints,
    PriceError,
    PriceErrorCode,
    PriceVerification,
    ProductRef,
    ProductSnapshot,
    as_price,
    quantize_price,
)

REF = ProductRef(
    platform="taobao", shop_id="SHOP-01", product_id="ITEM-1001", sku_id="SKU-001"
)


# ---------------------------------------------------------------------------
# 五个领域对象
# ---------------------------------------------------------------------------


def test_product_ref_fields_and_normalization():
    assert {f.name for f in fields(ProductRef)} == {
        "platform",
        "shop_id",
        "product_id",
        "sku_id",
    }
    ref = ProductRef(
        platform=" Taobao ", shop_id="SHOP-01", product_id="ITEM-1001", sku_id="SKU-001"
    )
    assert ref.platform == "taobao"
    assert ref.key == "taobao:SHOP-01:ITEM-1001:SKU-001"


@pytest.mark.parametrize("missing", ["platform", "shop_id", "product_id", "sku_id"])
def test_product_ref_rejects_empty_dimension(missing):
    kwargs = {
        "platform": "taobao",
        "shop_id": "SHOP-01",
        "product_id": "ITEM-1001",
        "sku_id": "SKU-001",
    }
    kwargs[missing] = "  "
    with pytest.raises(ValueError):
        ProductRef(**kwargs)


def test_product_snapshot_carries_price_stock_status_activity():
    snapshot = ProductSnapshot(
        product_ref=REF,
        name="海洋之风法式泡泡袖连衣裙",
        current_price="89.00",
        stock=1523,
        status="on_sale",
        activity_name="双11预热 9折",
        activity_locked=True,
        observed_at="2026-09-16T10:00:00+08:00",
    )
    assert isinstance(snapshot.current_price, Decimal)
    assert snapshot.current_price == Decimal("89.00")
    assert snapshot.stock == 1523
    assert snapshot.status == "on_sale"
    assert snapshot.activity_locked is True


def test_price_constraints_carry_cost_and_scale():
    constraints = PriceConstraints(
        product_ref=REF,
        cost_price="59.00",
        min_price="65.00",
        max_price="199.00",
        price_scale=2,
        locked_by_activity=False,
        source="cost_provider",
    )
    assert constraints.cost_price == Decimal("59.00")
    assert constraints.price_scale == 2
    assert constraints.source == "cost_provider"


def test_price_verification_holds_expected_and_observed():
    verification = PriceVerification(
        product_ref=REF,
        expected_price="89.00",
        observed_price="89.00",
        consistent=True,
        attempts=2,
        detail="平台回查一致",
    )
    assert verification.consistent is True
    assert verification.observed_price == Decimal("89.00")
    assert verification.attempts == 2


def test_price_verification_allows_unknown_observed():
    verification = PriceVerification(
        product_ref=REF,
        expected_price=Decimal("89"),
        observed_price=None,
        consistent=False,
        detail="回查超时",
    )
    assert verification.observed_price is None
    assert verification.consistent is False


# ---------------------------------------------------------------------------
# PriceChangeCommand：领域命令的边界
# ---------------------------------------------------------------------------


def test_price_change_command_required_fields():
    command = PriceChangeCommand(
        operation_id="op-1",
        product_ref=REF,
        target_price=Decimal("89.00"),
        requester="manager",
        reason="清库存",
    )
    assert command.operation_id == "op-1"
    assert command.product_ref is REF
    assert command.target_price == Decimal("89.00")
    assert command.requester == "manager"
    assert command.reason == "清库存"
    assert {f.name for f in fields(PriceChangeCommand)} == {
        "operation_id",
        "product_ref",
        "target_price",
        "requester",
        "reason",
    }


def test_price_change_command_rejects_platform_private_fields():
    """领域命令不得携带签名 / token / 成本价等平台私有字段。"""
    names = {f.name for f in fields(PriceChangeCommand)}
    forbidden = {
        "sign",
        "signature",
        "access_token",
        "app_key",
        "app_secret",
        "timestamp",
        "nonce",
        "session",
        "cost_price",
        "platform_code",
        "shop_token",
    }
    assert not (names & forbidden), f"领域命令泄漏平台私有字段: {sorted(names & forbidden)}"


def test_target_price_must_be_decimal():
    """目标价拒绝 float —— 二进制浮点在比价/回查处会留尾差。"""
    assert isinstance(
        PriceChangeCommand(
            operation_id="op-1",
            product_ref=REF,
            target_price="89.10",
            requester="manager",
        ).target_price,
        Decimal,
    )
    with pytest.raises(TypeError):
        PriceChangeCommand(
            operation_id="op-1",
            product_ref=REF,
            target_price=89.1,
            requester="manager",
        )


def test_price_change_command_requires_operation_id_and_requester():
    with pytest.raises(ValueError):
        PriceChangeCommand(
            operation_id="  ", product_ref=REF, target_price=Decimal("89"), requester="manager"
        )
    with pytest.raises(ValueError):
        PriceChangeCommand(
            operation_id="op-1", product_ref=REF, target_price=Decimal("89"), requester=""
        )


def test_target_price_rejects_negative():
    with pytest.raises(ValueError):
        PriceChangeCommand(
            operation_id="op-1", product_ref=REF, target_price=Decimal("-1"), requester="manager"
        )


def test_as_price_and_quantize():
    assert as_price("89.005") == Decimal("89.005")
    with pytest.raises(TypeError):
        as_price(89.005)
    assert quantize_price(Decimal("89.005"), 2) == Decimal("89.01")
    assert quantize_price(Decimal("89.4"), 0) == Decimal("89")


# ---------------------------------------------------------------------------
# 统一平台错误类型
# ---------------------------------------------------------------------------


def test_price_error_code_covers_unified_classification():
    assert PriceErrorCode.CLIENT_ERROR.value == "CLIENT_ERROR"
    assert PriceErrorCode.BUSINESS_ERROR.value == "BUSINESS_ERROR"
    assert PriceErrorCode.TRANSIENT_ERROR.value == "TRANSIENT_ERROR"
    assert PriceErrorCode.FATAL_ERROR.value == "FATAL_ERROR"
    assert PriceErrorCode.UNKNOWN_OUTCOME.value == "UNKNOWN_OUTCOME"
    assert PriceErrorCode.CAPABILITY_UNSUPPORTED.value == "CAPABILITY_UNSUPPORTED"
    assert PriceErrorCode.TRANSIENT_ERROR.retryable is True
    assert PriceErrorCode.BUSINESS_ERROR.retryable is False


def test_price_error_keeps_platform_context_out_of_domain_command():
    error = PriceError(
        PriceErrorCode.BUSINESS_ERROR,
        "价格低于成本价",
        platform="taobao",
        platform_code=15,
        side_effect_possible=False,
    )
    assert error.code is PriceErrorCode.BUSINESS_ERROR
    assert error.platform == "taobao"
    assert error.platform_code == 15
    assert error.retryable is False

    unknown = PriceError(PriceErrorCode.UNKNOWN_OUTCOME, "写入后客户端超时", side_effect_possible=True)
    assert unknown.side_effect_possible is True


# ---------------------------------------------------------------------------
# 平台能力画像
# ---------------------------------------------------------------------------


def test_platform_capabilities_required_dimensions():
    names = {f.name for f in fields(PlatformCapabilities)}
    assert {
        "query_snapshot",
        "update_price",
        "verify_price",
        "price_scale",
        "requires_signature",
    } <= names


def test_taobao_and_douyin_have_different_profiles():
    taobao = get_capabilities("taobao")
    douyin = get_capabilities("douyin")
    assert taobao.platform == "taobao"
    assert douyin.platform == "douyin"
    # 两个平台都支持调价闭环，但精度约束不同（元/两位小数 vs 分/整数）
    assert taobao.price_scale == 2
    assert douyin.price_scale == 0
    assert taobao.requires_signature is True
    assert douyin.requires_signature is True
    assert taobao != douyin


def test_mock_is_offline_test_platform_only():
    mock = get_capabilities("mock")
    assert mock.requires_signature is False
    assert mock.query_snapshot and mock.update_price and mock.verify_price
    assert mock.label.startswith("本地 Mock")


def test_unknown_platform_raises_capability_unsupported():
    with pytest.raises(PriceError) as excinfo:
        get_capabilities("open")
    assert excinfo.value.code is PriceErrorCode.CAPABILITY_UNSUPPORTED


def test_missing_capability_returns_capability_unsupported():
    """平台存在但不支持某能力时，不静默降级，返回 CAPABILITY_UNSUPPORTED。"""
    PLATFORM_CAPABILITIES["legacy_shop"] = PlatformCapabilities(
        platform="legacy_shop",
        query_snapshot=True,
        update_price=False,
        verify_price=False,
        price_scale=2,
        requires_signature=False,
        label="老平台（只读）",
    )
    try:
        assert require_capability("legacy_shop", "query_snapshot").update_price is False
        with pytest.raises(PriceError) as excinfo:
            require_capability("legacy_shop", "update_price")
        assert excinfo.value.code is PriceErrorCode.CAPABILITY_UNSUPPORTED
    finally:
        PLATFORM_CAPABILITIES.pop("legacy_shop", None)


def test_capability_profile_is_immutable():
    taobao = get_capabilities("taobao")
    with pytest.raises(Exception):
        taobao.update_price = False  # type: ignore[misc]
