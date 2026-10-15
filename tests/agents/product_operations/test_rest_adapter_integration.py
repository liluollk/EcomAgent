"""通过组合根验证 REST CommerceAdapter 进入现有 Runtime 写入安全链。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from adapters.commerce.rest_adapter import RestCommerceAdapter
from runtime.backend.protocol import BackendCompleted, ToolCallCompleted
from runtime.interfaces.state import RunStatus


@pytest.fixture()
async def commerce_adapter_for_application() -> AsyncIterator[RestCommerceAdapter]:
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        operation_id = request.headers["Idempotency-Key"].split(":", 1)[1]
        return httpx.Response(
            200,
            json={
                "status": "APPLIED",
                "operation_id": operation_id,
                "before_price": "128.00",
                "after_price": body["price"],
                "listing_version": 2,
                "external_ref": "rest-operation-1",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    adapter = RestCommerceAdapter(
        base_url="https://commerce.test/v1",
        allowed_hosts=frozenset({"commerce.test"}),
        access_token="test-secret",
        client=client,
    )
    adapter.requests = requests
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def test_injected_rest_adapter_runs_after_runtime_confirmation(
    product_application, commerce_adapter_for_application: RestCommerceAdapter
) -> None:
    product_application.scripted_backend.set_rounds(
        [
            [
                ToolCallCompleted(
                    call_id="price-call",
                    name="set_listing_price",
                    arguments={
                        "store_ref": "淘宝旗舰店",
                        "sku": "SKU-001",
                        "target_price": 99,
                    },
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ]
        ]
    )

    assert product_application.adapter is commerce_adapter_for_application
    run = await product_application.start("把淘宝旗舰店 SKU-001 改成 99 元")
    assert run.status == RunStatus.WAITING_CONFIRMATION
    assert commerce_adapter_for_application.requests == []

    run = await product_application.confirm(run.pending_confirmation_id)

    assert run.status == RunStatus.COMPLETED
    assert len(commerce_adapter_for_application.requests) == 1
    request = commerce_adapter_for_application.requests[0]
    assert request.method == "PATCH"
    assert request.url.path.endswith("/price")
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert request.headers["Idempotency-Key"].startswith("op:")
    assert json.loads(request.content) == {
        "price": "99.00",
        "currency": "CNY",
        "expected_version": 1,
    }
    snapshot = await product_application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    assert snapshot.status == "SUCCEEDED"
    assert snapshot.operation_id is not None
