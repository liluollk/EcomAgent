"""双平台调价 Adapter 测试（Task 3）— 淘宝 TOP 信封 vs 抖店 JSON 信封。

核心断言：同一个业务意图（target_price=89.00）经过两个 Adapter 后，
请求形态与价格单位不同（淘宝元字符串 / 抖店分整数），但解析出的领域结果一致
（receipt.applied_price == 89.00，回查 consistent is True）；且两套完全不同的
平台错误码都能映射到同一套统一 PriceErrorCode。

全部走 in-process ASGI（mock_commerce.routes.app），不发真实网络请求。
"""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal

import httpx
import pytest

from integrations.commerce.adapter import DouyinAdapter, TaobaoAdapter, _BasePriceAdapter
from integrations.commerce.client import ChannelRestClient
from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceError,
    PriceErrorCode,
    PricePlatform,
    ProductRef,
)
from mock_commerce import fault_injection
from mock_commerce.domain import reset_idempotency
from mock_commerce.routes import app
from mock_commerce.store import (
    PRODUCT_STATE,
    reset_product_state,
    reset_writes,
    writes_of,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(platform: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, f"{platform}_price_contract.json"), encoding="utf-8") as f:
        return json.load(f)


def _client(timeout: float = 5.0) -> ChannelRestClient:
    return ChannelRestClient(
        base_url="http://t",
        transport=httpx.ASGITransport(app=app),
        timeout=timeout,
    )


def _ref(platform: str, sku: str = "SKU-002") -> ProductRef:
    fx = _load(platform)
    r = fx["ref"]
    return ProductRef(platform=platform, shop_id=r["shop_id"], product_id=r["product_id"], sku_id=sku)


def _cmd(platform: str, sku: str = "SKU-002", target: str = "89.00") -> PriceChangeCommand:
    return PriceChangeCommand(
        operation_id=f"op-{platform}-{sku}",
        product_ref=_ref(platform, sku),
        target_price=Decimal(target),
        requester="manager",
        reason="调价测试",
    )


@pytest.fixture(autouse=True)
def _reset_state():
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    reset_product_state()
    # PRODUCT_STATE 可能被测试改价篡改，恢复出厂值（含跨平台共享状态）
    yield
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    reset_product_state()


# ---------------------------------------------------------------------------
# 不再是 stub：淘宝 / 抖店都实现 PricePlatform 三动作
# ---------------------------------------------------------------------------


def test_taobao_adapter_is_real_price_platform():
    a = TaobaoAdapter()
    assert isinstance(a, _BasePriceAdapter)
    assert isinstance(a, PricePlatform)
    for name in ("query_snapshot", "apply_price", "verify_price"):
        assert callable(getattr(a, name))
    result = asyncio.run(a.probe({"platform": "taobao"}))
    assert result["ok"] is True


def test_douyin_adapter_is_real_price_platform():
    a = DouyinAdapter()
    assert isinstance(a, _BasePriceAdapter)
    assert isinstance(a, PricePlatform)
    for name in ("query_snapshot", "apply_price", "verify_price"):
        assert callable(getattr(a, name))
    result = asyncio.run(a.probe({"platform": "douyin"}))
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# 请求形态差异：信封字段 + 价格单位（淘宝元字符串 / 抖店分整数）
# ---------------------------------------------------------------------------


def test_build_request_envelope_shapes_differ():
    tb = TaobaoAdapter().build_request("apply_price", None, {})
    dy = DouyinAdapter().build_request("apply_price", None, {})

    # 淘宝：单端点 + TOP 信封 method 分发
    assert tb[0] == "POST" and tb[1] == "/taobao/top/api"
    tb_env = tb[2]["json_body"]
    assert tb_env["method"] == "taobao.item.sku.price.update"
    assert tb_env["sign_method"] == "hmac-sha256"
    assert isinstance(tb_env["sign"], str) and tb_env["sign"].isupper()

    # 抖店：独立端点 + access_token，无 method 字段
    assert dy[0] == "POST" and dy[1] == "/douyin/product/sku/price"
    dy_env = dy[2]["json_body"]
    assert "method" not in dy_env
    assert dy_env["access_token"] == "mock_access_token"
    assert isinstance(dy_env["sign"], str)


def test_price_unit_differs_but_resolves_same():
    # 价格单位差异体现在 adapter 构造的请求信封里（apply_price 内部用 _build_envelope）
    tb = TaobaoAdapter()
    tb_env = tb._build_envelope(
        tb._method_update, {**tb._ref_params(_ref("taobao")), "price": tb._price_out(Decimal("89.00"))}
    )
    dy = DouyinAdapter()
    dy_env = dy._build_envelope(
        dy._method_update, {**dy._ref_params(_ref("douyin")), "price": dy._price_out(Decimal("89.00"))}
    )
    # 淘宝以「元·两位小数字符串」传递 89.00；抖店以「分·整数」传递 8900
    assert tb_env["price"] == "89.00"
    assert dy_env["price"] == 8900


# ---------------------------------------------------------------------------
# 关键断言：同一业务意图 → 解析出的领域结果一致
# ---------------------------------------------------------------------------


async def test_same_command_cross_platform_consistent_result():
    for platform, key in (("taobao", "idem-tb-x"), ("douyin", "idem-dy-x")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            cmd = _cmd(platform)
            receipt = await adapter.apply_price(cmd, idempotency_key=key)
            assert receipt.applied_price == Decimal("89.00"), platform
            verify = await adapter.verify_price(cmd.product_ref, Decimal("89.00"))
            assert verify.consistent is True, platform
        finally:
            await client.aclose()


async def test_snapshot_reports_activity_lock_for_sku001():
    # 淘宝/抖店 SKU-001 处于促销活动、活动锁价（驱动活动锁价拒绝）
    for platform, key in (("taobao", "idem-tb-lock"), ("douyin", "idem-dy-lock")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            snap = await adapter.query_snapshot(_ref(platform, "SKU-001"))
            assert snap.activity_locked is True, platform
            assert snap.activity_name != "", platform
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# 错误码映射：两套完全不同的平台码 → 同一个 PriceErrorCode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["taobao", "douyin"])
def test_error_code_mapping_from_fixture(platform):
    fx = _load(platform)
    adapter_cls = TaobaoAdapter if platform == "taobao" else DouyinAdapter
    for entry in fx["error_code_mapping"]:
        mapped = adapter_cls._map_code(entry["code"])
        assert mapped.value == entry["expected"], (
            f"{platform} code {entry['code']} ({entry['name']}) "
            f"期望 {entry['expected']} 实得 {mapped.value}"
        )


async def test_below_cost_rejected_as_business_error():
    # 成本价高于 10.00，低于成本写入必须被平台拒绝（BUSINESS_ERROR）
    for platform, key in (("taobao", "idem-tb-cost"), ("douyin", "idem-dy-cost")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            cmd = _cmd(platform, target="10.00")
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.BUSINESS_ERROR, platform
        finally:
            await client.aclose()


async def test_activity_lock_rejected_as_business_error():
    for platform, key in (("taobao", "idem-tb-act"), ("douyin", "idem-dy-act")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            cmd = _cmd(platform, sku="SKU-001")
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.BUSINESS_ERROR, platform
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# 确定性故障脚本（Task 5）：能力拒绝 / 持续业务错误 / 限流 / 畸形 / 超时 / 回查
# ---------------------------------------------------------------------------


async def test_capability_refused_maps_to_unsupported():
    for platform, key in (("taobao", "idem-tb-cap"), ("douyin", "idem-dy-cap")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("capability_refused")
            cmd = _cmd(platform)
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.CAPABILITY_UNSUPPORTED, platform
        finally:
            await client.aclose()


async def test_business_error_permanent():
    for platform, key in (("taobao", "idem-tb-biz"), ("douyin", "idem-dy-biz")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("business_error_permanent")
            cmd = _cmd(platform)
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.BUSINESS_ERROR, platform
        finally:
            await client.aclose()


async def test_platform_rate_limit_once_then_success():
    for platform, key in (("taobao", "idem-tb-rl"), ("douyin", "idem-dy-rl")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("platform_rate_limit_once_then_success")
            cmd = _cmd(platform)
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.TRANSIENT_ERROR, platform
            # 第二次恢复正常
            receipt = await adapter.apply_price(cmd, idempotency_key=key + "-2")
            assert receipt.applied_price == Decimal("89.00"), platform
        finally:
            await client.aclose()


async def test_platform_malformed_once_then_success():
    for platform, key in (("taobao", "idem-tb-mf"), ("douyin", "idem-dy-mf")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("platform_malformed_once_then_success")
            cmd = _cmd(platform)
            with pytest.raises(PriceError):
                await adapter.apply_price(cmd, idempotency_key=key)
            receipt = await adapter.apply_price(cmd, idempotency_key=key + "-2")
            assert receipt.applied_price == Decimal("89.00"), platform
        finally:
            await client.aclose()


async def test_write_timeout_before_commit_no_side_effect():
    for platform, key in (("taobao", "idem-tb-wtb"), ("douyin", "idem-dy-wtb")):
        client = _client(timeout=0.3)
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("write_timeout_before_commit", timeout_seconds=1.0)
            cmd = _cmd(platform)
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.TRANSIENT_ERROR, platform
            # 副作用落库「前」超时 → 不得产生写副作用
            op = f"{platform}_price_update"
            assert len(writes_of(op)) == 0, platform
        finally:
            await client.aclose()


async def test_write_timeout_after_commit_exactly_one_side_effect():
    for platform, key in (("taobao", "idem-tb-wta"), ("douyin", "idem-dy-wta")):
        client = _client(timeout=0.3)
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        op = f"{platform}_price_update"
        try:
            fault_injection.load_script("write_timeout_after_commit", timeout_seconds=1.0)
            cmd = _cmd(platform)
            # 写入后超时：客户端超时，但副作用已落库
            with pytest.raises(PriceError) as ei:
                await adapter.apply_price(cmd, idempotency_key=key)
            assert ei.value.code is PriceErrorCode.TRANSIENT_ERROR, platform
            # 写超时必须标记「副作用可能已发生」
            assert ei.value.side_effect_possible is True, platform
            assert len(writes_of(op)) == 1, platform
            # 同一幂等键重试：不得新增第二条副作用（回放首次结果）
            receipt = await adapter.apply_price(cmd, idempotency_key=key)
            assert receipt.idempotent_replay is True, platform
            assert len(writes_of(op)) == 1, platform
        finally:
            await client.aclose()


async def test_verify_mismatch():
    for platform, key in (("taobao", "idem-tb-vm"), ("douyin", "idem-dy-vm")):
        client = _client()
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("verify_mismatch")
            verify = await adapter.verify_price(_ref(platform, "SKU-002"), Decimal("89.00"))
            assert verify.consistent is False, platform
        finally:
            await client.aclose()


async def test_verify_timeout():
    for platform, key in (("taobao", "idem-tb-vt"), ("douyin", "idem-dy-vt")):
        client = _client(timeout=0.3)
        adapter = TaobaoAdapter(client=client) if platform == "taobao" else DouyinAdapter(client=client)
        try:
            fault_injection.load_script("verify_timeout", timeout_seconds=1.0)
            with pytest.raises(PriceError) as ei:
                await adapter.verify_price(_ref(platform, "SKU-002"), Decimal("89.00"))
            assert ei.value.code is PriceErrorCode.TRANSIENT_ERROR, platform
            assert ei.value.side_effect_possible is False, platform
        finally:
            await client.aclose()
