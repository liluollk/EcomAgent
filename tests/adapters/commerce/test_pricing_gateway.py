"""PricingGateway 平台实现的直接测试(P1 Task 6;设计 §5.3)。

Mock 与 REST 两个 Adapter 现在把 ``get_price`` / ``set_price`` /
``reconcile_price`` 作为主接口(旧 ``execute`` / ``reconcile`` 退化为兼容包装);
本文件直接覆盖新接口,不经过旧回执类型。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest

from adapters.commerce.mock_adapter import MockCommerceAdapter
from adapters.commerce.rest_adapter import RestCommerceAdapter
from agents.product_operations.persistence.models import Listing
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    PriceUnavailable,
    PriceWriteReceipt,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.shared.write_contracts import WriteStatus
from runtime.execution.reconciliation import ReconcileResult


def _set_price_command(
    listing: Listing,
    *,
    operation_id: str,
    target_price: Decimal = Decimal("99.00"),
    expected_version: int | None = None,
) -> SetListingPriceCommand:
    return SetListingPriceCommand(
        operation_id=operation_id,
        idempotency_key=f"set_listing_price:{operation_id}",
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        sku="SKU-1",
        target_price=target_price,
        currency=listing.currency,
        expected_version=listing.version if expected_version is None else expected_version,
    )


def _price_query(listing: Listing) -> PriceQuery:
    return PriceQuery(
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        currency=listing.currency,
    )


def _reconcile_command(
    listing: Listing, *, operation_id: str, expected_price: Decimal
) -> ReconcilePriceCommand:
    return ReconcilePriceCommand(
        operation_id=operation_id,
        workspace_id=listing.workspace_id,
        store_id=listing.store_id,
        listing_id=listing.id,
        external_store_ref=str(listing.store_id),
        external_product_ref=listing.external_product_ref,
        expected_price=expected_price,
        currency=listing.currency,
    )


# ---- MockCommerceAdapter ----


async def test_mock_get_price_returns_authoritative_snapshot(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    snapshot = await mock_adapter.get_price(_price_query(listing))
    assert snapshot.price == Decimal("128.00")
    assert snapshot.currency == listing.currency
    assert snapshot.listing_version == listing.version
    assert snapshot.external_ref == listing.external_product_ref


async def test_mock_get_price_raises_when_listing_is_missing(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    query = _price_query(listing)
    missing = PriceQuery(
        workspace_id=query.workspace_id,
        store_id=query.store_id,
        listing_id=uuid.uuid4(),
        external_store_ref=query.external_store_ref,
        external_product_ref=query.external_product_ref,
        currency=query.currency,
    )
    with pytest.raises(PriceUnavailable):
        await mock_adapter.get_price(missing)


async def test_mock_set_price_observes_before_after_and_version(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    previous_version = listing.version
    receipt = await mock_adapter.set_price(
        _set_price_command(listing, operation_id="op-1")
    )
    assert isinstance(receipt, PriceWriteReceipt)
    assert receipt.status is WriteStatus.APPLIED
    assert receipt.before_price == Decimal("128.00")
    assert receipt.after_price == Decimal("99.00")
    assert receipt.resource_version == previous_version + 1
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_mock_set_price_replays_same_operation_without_second_write(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    command = _set_price_command(listing, operation_id="op-1")
    first = await mock_adapter.set_price(command)
    replay = await mock_adapter.set_price(command)
    assert replay == first
    assert await mock_adapter.count_logical_writes("op-1") == 1


async def test_mock_reconcile_price_recovers_after_response_loss(
    mock_adapter: MockCommerceAdapter, listing: Listing
) -> None:
    mock_adapter.faults.inject("response_lost_after_commit")
    receipt = await mock_adapter.set_price(
        _set_price_command(listing, operation_id="op-1")
    )
    assert receipt.status is WriteStatus.UNKNOWN
    assert receipt.before_price is None and receipt.after_price is None

    mock_adapter.faults.clear()
    applied = await mock_adapter.reconcile_price(
        _reconcile_command(listing, operation_id="op-1", expected_price=Decimal("99.00"))
    )
    assert applied is ReconcileResult.APPLIED


# ---- RestCommerceAdapter ----


def _make_rest_adapter(handler, **kwargs) -> RestCommerceAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RestCommerceAdapter(
        base_url="https://commerce.example.test/v1",
        allowed_hosts=frozenset({"commerce.example.test"}),
        access_token="test-secret",
        client=client,
        **kwargs,
    )


async def test_rest_set_price_uses_injected_idempotency_key(
    listing: Listing,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "operation_id": "op-1",
                "status": "APPLIED",
                "before_price": "128.00",
                "after_price": "99.00",
                "listing_version": 8,
                "external_ref": "change-42",
            },
        )

    adapter = _make_rest_adapter(handler)
    try:
        receipt = await adapter.set_price(
            _set_price_command(listing, operation_id="op-1")
        )
        assert receipt.status is WriteStatus.APPLIED
        assert receipt.before_price == Decimal("128.00")
        assert receipt.after_price == Decimal("99.00")
        assert receipt.resource_version == 8
        assert len(requests) == 1
        assert requests[0].method == "PATCH"
        assert requests[0].headers["Idempotency-Key"] == "set_listing_price:op-1"
    finally:
        await adapter.aclose()


async def test_rest_set_price_response_loss_is_unknown_without_observation(
    listing: Listing,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response timed out")

    adapter = _make_rest_adapter(handler)
    try:
        receipt = await adapter.set_price(
            _set_price_command(listing, operation_id="op-1")
        )
        assert receipt.status is WriteStatus.UNKNOWN
        assert receipt.before_price is None
        assert receipt.after_price is None
        assert receipt.resource_version is None
    finally:
        await adapter.aclose()


async def test_rest_get_price_reads_snapshot(listing: Listing) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "price": "128.00",
                "currency": listing.currency,
                "listing_version": 7,
                "external_ref": listing.external_product_ref,
            },
        )

    adapter = _make_rest_adapter(handler)
    try:
        snapshot = await adapter.get_price(_price_query(listing))
        assert snapshot.price == Decimal("128.00")
        assert snapshot.listing_version == 7
        assert snapshot.external_ref == listing.external_product_ref
        assert requests[0].method == "GET"
        assert requests[0].url.path.endswith("/price")
    finally:
        await adapter.aclose()


async def test_rest_get_price_is_unavailable_on_error_and_currency_mismatch(
    listing: Listing,
) -> None:
    responses = iter(
        (
            httpx.Response(503),
            httpx.Response(
                200,
                json={
                    "price": "128.00",
                    "currency": "USD",
                    "listing_version": 7,
                    "external_ref": listing.external_product_ref,
                },
            ),
        )
    )
    adapter = _make_rest_adapter(lambda request: next(responses))
    try:
        with pytest.raises(PriceUnavailable):
            await adapter.get_price(_price_query(listing))
        with pytest.raises(PriceUnavailable):
            await adapter.get_price(_price_query(listing))
    finally:
        await adapter.aclose()


async def test_rest_reconcile_price_maps_not_applied_and_applied(
    listing: Listing,
) -> None:
    responses = iter(
        (
            httpx.Response(404),
            httpx.Response(
                200,
                json={
                    "operation_id": "op-1",
                    "status": "APPLIED",
                    "external_store_ref": str(listing.store_id),
                    "external_product_ref": listing.external_product_ref,
                    "price": "99.00",
                    "currency": listing.currency,
                },
            ),
        )
    )
    adapter = _make_rest_adapter(lambda request: next(responses))
    try:
        command = _reconcile_command(
            listing, operation_id="op-1", expected_price=Decimal("99.00")
        )
        assert await adapter.reconcile_price(command) is ReconcileResult.NOT_APPLIED
        assert await adapter.reconcile_price(command) is ReconcileResult.APPLIED
    finally:
        await adapter.aclose()
