"""固定商品 REST 协议的确定性测试。"""

import json
import uuid
from decimal import Decimal

import httpx
import pytest

from adapters.commerce.protocol import (
    ExecutionStatus,
    ReconcileResult,
    ReconciliationCommand,
    UpdatePriceCommand,
)
from adapters.commerce.rest_adapter import RestCommerceAdapter


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
