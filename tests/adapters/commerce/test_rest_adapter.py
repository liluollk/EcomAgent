"""固定商品 REST 协议的确定性测试。"""

import json
import uuid
from decimal import Decimal

import httpx
import pytest

from adapters.commerce import CAPABILITIES, CapabilityUnsupportedError
from adapters.commerce.protocol import (
    ExecutionStatus,
    ReconcileResult,
    ReconciliationCommand,
    UpdatePriceCommand,
)
from adapters.commerce.rest_adapter import RestCommerceAdapter
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    InventoryQuery,
    ReconcileInventoryCommand,
)
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingQuery,
    ReconcileListingCommand,
)
from agents.product_operations.shared.write_contracts import WriteStatus


def update_command() -> UpdatePriceCommand:
    return UpdatePriceCommand(
        operation_id="op-123",
        workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        store_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        listing_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        external_store_ref="store-1",
        external_product_ref="item-9",
        target_price=Decimal("99.00"),
        currency="CNY",
        expected_version=7,
    )


def reconcile_command() -> ReconciliationCommand:
    return ReconciliationCommand(
        operation_id="op-123",
        workspace_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        store_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        listing_id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
        external_store_ref="store-1",
        external_product_ref="item-9",
        expected_price=Decimal("99.00"),
        currency="CNY",
    )


def make_adapter(handler, **kwargs) -> RestCommerceAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RestCommerceAdapter(
        base_url="https://commerce.example.test/v1",
        allowed_hosts=frozenset({"commerce.example.test"}),
        access_token="test-secret",
        client=client,
        **kwargs,
    )


async def test_execute_maps_fixed_request_and_normalizes_receipt() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "operation_id": "op-123",
                "status": "APPLIED",
                "before_price": "128.00",
                "after_price": "99.00",
                "listing_version": 8,
                "external_ref": "change-42",
            },
        )

    adapter = make_adapter(handler)
    try:
        receipt = await adapter.execute(update_command())
        assert receipt.status is ExecutionStatus.APPLIED
        assert receipt.before_price == Decimal("128.00")
        assert receipt.after_price == Decimal("99.00")
        assert receipt.listing_version == 8
        assert len(requests) == 1
        assert requests[0].method == "PATCH"
        assert requests[0].url.path == "/v1/stores/store-1/listings/item-9/price"
        assert requests[0].headers["Idempotency-Key"] == "update_price:op-123"
        assert requests[0].headers["Authorization"] == "Bearer test-secret"
        assert json.loads(requests[0].content) == {
            "price": "99.00",
            "currency": "CNY",
            "expected_version": 7,
        }
    finally:
        await adapter.aclose()


async def test_uncertain_write_is_not_retried_and_reconcile_is_operation_scoped() -> None:
    writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        if request.method == "PATCH":
            writes += 1
            raise httpx.ReadTimeout("response timed out")
        return httpx.Response(
            200,
            json={
                "operation_id": "op-123",
                "status": "APPLIED",
                "external_store_ref": "store-1",
                "external_product_ref": "item-9",
                "price": "99.00",
                "currency": "CNY",
            },
        )

    adapter = make_adapter(handler)
    try:
        receipt = await adapter.execute(update_command())
        assert receipt.status is ExecutionStatus.UNKNOWN
        assert writes == 1
        assert await adapter.reconcile(reconcile_command()) is ReconcileResult.APPLIED
        assert writes == 1
    finally:
        await adapter.aclose()


async def test_reconcile_rejects_response_for_different_resource() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "operation_id": "op-123",
                "status": "APPLIED",
                "external_store_ref": "another-store",
                "external_product_ref": "item-9",
                "price": "99.00",
                "currency": "CNY",
            },
        )

    adapter = make_adapter(handler)
    try:
        assert await adapter.reconcile(reconcile_command()) is ReconcileResult.UNKNOWN
    finally:
        await adapter.aclose()


async def test_version_conflict_is_definite_and_redirect_is_not_followed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(409, json={"reason": "VERSION_CONFLICT"})
        return httpx.Response(307, headers={"Location": "https://other.example/steal"})

    adapter = make_adapter(handler)
    try:
        conflict = await adapter.execute(update_command())
        assert conflict.status is ExecutionStatus.FAILED
        assert conflict.reason_code == "VERSION_CONFLICT"
        redirect = await adapter.execute(update_command())
        assert redirect.status is ExecutionStatus.UNKNOWN
        assert len(requests) == 2
    finally:
        await adapter.aclose()


async def test_platform_rejection_and_server_error_have_distinct_outcomes() -> None:
    responses = iter((httpx.Response(400), httpx.Response(503)))
    adapter = make_adapter(lambda request: next(responses))
    try:
        rejected = await adapter.execute(update_command())
        unavailable = await adapter.execute(update_command())
        assert rejected.status is ExecutionStatus.FAILED
        assert rejected.reason_code == "PLATFORM_REJECTED"
        assert unavailable.status is ExecutionStatus.UNKNOWN
    finally:
        await adapter.aclose()


async def test_oversized_response_is_unknown_and_reconcile_not_found_is_not_applied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(200, content=b"x" * 32)
        return httpx.Response(404)

    adapter = make_adapter(handler, max_response_bytes=16)
    try:
        receipt = await adapter.execute(update_command())
        assert receipt.status is ExecutionStatus.UNKNOWN
        assert await adapter.reconcile(reconcile_command()) is ReconcileResult.NOT_APPLIED
    finally:
        await adapter.aclose()


async def test_invalid_endpoint_is_rejected_and_secret_is_not_exposed() -> None:
    with pytest.raises(ValueError):
        RestCommerceAdapter(
            base_url="http://commerce.example.test/v1",
            allowed_hosts=frozenset({"commerce.example.test"}),
            access_token="test-secret",
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed", request=request)

    adapter = make_adapter(handler)
    try:
        receipt = await adapter.execute(update_command())
        assert receipt.status is ExecutionStatus.UNKNOWN
        assert "test-secret" not in (receipt.reason_code or "")
    finally:
        await adapter.aclose()


# ---- 计划 P1 Task 9:能力范围只声明定价 ----

_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_STORE_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_LISTING_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_LOCATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_PRODUCT_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")


def inventory_query() -> InventoryQuery:
    return InventoryQuery(
        workspace_id=_WORKSPACE_ID,
        store_id=_STORE_ID,
        location_id=_LOCATION_ID,
        product_id=_PRODUCT_ID,
        external_store_ref="store-1",
        external_location_ref="loc-1",
        external_product_ref="item-9",
    )


def inventory_command() -> AdjustInventoryCommand:
    return AdjustInventoryCommand(
        operation_id="op-123",
        idempotency_key="adjust_inventory_quantity:op-123",
        workspace_id=_WORKSPACE_ID,
        store_id=_STORE_ID,
        location_id=_LOCATION_ID,
        product_id=_PRODUCT_ID,
        external_store_ref="store-1",
        external_location_ref="loc-1",
        external_product_ref="item-9",
        sku="SKU-1",
        delta=1,
        reason="MANUAL",
        expected_version=7,
        quantity_before=20,
    )


def inventory_reconcile_command() -> ReconcileInventoryCommand:
    return ReconcileInventoryCommand(
        operation_id="op-123",
        workspace_id=_WORKSPACE_ID,
        store_id=_STORE_ID,
        location_id=_LOCATION_ID,
        product_id=_PRODUCT_ID,
        external_store_ref="store-1",
        external_location_ref="loc-1",
        external_product_ref="item-9",
        quantity_before=20,
        expected_quantity_after=21,
    )


def listing_query() -> ListingQuery:
    return ListingQuery(
        workspace_id=_WORKSPACE_ID,
        store_id=_STORE_ID,
        listing_id=_LISTING_ID,
        external_store_ref="store-1",
        external_product_ref="item-9",
    )


def publication_command() -> ChangePublicationCommand:
    return ChangePublicationCommand(
        operation_id="op-123",
        idempotency_key="change_listing_publication:op-123",
        workspace_id=_WORKSPACE_ID,
        store_id=_STORE_ID,
        listing_id=_LISTING_ID,
        external_store_ref="store-1",
        external_product_ref="item-9",
        sku="SKU-1",
        reason=None,
        expected_version=7,
    )


def listing_reconcile_command() -> ReconcileListingCommand:
    return ReconcileListingCommand(
        operation_id="op-123",
        workspace_id=_WORKSPACE_ID,
        store_id=_STORE_ID,
        listing_id=_LISTING_ID,
        external_store_ref="store-1",
        external_product_ref="item-9",
        expected_state=ListingStatus.UNPUBLISHED,
    )


async def test_unsupported_capabilities_fail_closed_without_network() -> None:
    """库存/刊登不在 P1 的 REST 适配边界内:必须失败闭合,且绝不发请求。

    真实的第三方平台未必存在可写的库存/刊登端点。为了"凑齐接口"而猜测 URL 会把
    装配错误变成生产事故,因此每个方法都必须立刻失败:读抛
    :class:`CapabilityUnsupportedError`(``LookupError`` 家族,与 Port 的读失败
    通道一致),写返回 ``FAILED`` + ``CAPABILITY_UNSUPPORTED`` 回执,回查确定为
    ``NOT_APPLIED``。
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("unsupported capabilities must never reach the network")

    adapter = make_adapter(handler)
    try:
        assert adapter.supported_capabilities == CAPABILITIES
        assert CAPABILITIES == frozenset({"pricing"})

        # 读:失败通道与 Port 声明的 LookupError 家族一致,并携带稳定错误码。
        with pytest.raises(CapabilityUnsupportedError) as inventory_read:
            await adapter.get_inventory(inventory_query())
        assert isinstance(inventory_read.value, LookupError)
        assert inventory_read.value.reason_code == "CAPABILITY_UNSUPPORTED"
        assert inventory_read.value.capability == "inventory"

        with pytest.raises(CapabilityUnsupportedError) as listing_read:
            await adapter.get_listing(listing_query())
        assert listing_read.value.capability == "listing"

        # 写:确定失败回执,零观测(没有"写了一半"的可能)。
        inventory_receipt = await adapter.adjust_inventory(inventory_command())
        assert inventory_receipt.status is WriteStatus.FAILED
        assert inventory_receipt.reason_code == "CAPABILITY_UNSUPPORTED"
        assert inventory_receipt.after_quantity is None
        assert inventory_receipt.resource_version is None

        for receipt in (
            await adapter.publish_listing(publication_command()),
            await adapter.unpublish_listing(publication_command()),
        ):
            assert receipt.status is WriteStatus.FAILED
            assert receipt.reason_code == "CAPABILITY_UNSUPPORTED"
            assert receipt.resource_version is None
            assert receipt.changed is None

        # 回查:能力不支持的写不可能生效,确定为 NOT_APPLIED(同样不发请求)。
        assert (
            await adapter.reconcile_inventory(inventory_reconcile_command())
            is ReconcileResult.NOT_APPLIED
        )
        assert (
            await adapter.reconcile_listing(listing_reconcile_command())
            is ReconcileResult.NOT_APPLIED
        )

        assert requests == []
    finally:
        await adapter.aclose()
