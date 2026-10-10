"""固定商品 REST API 契约的 CommerceAdapter 实现。

该 Adapter 只允许受信任 HTTPS 主机上的商品改价和操作回查端点；URL 与
HTTP 方法不接受模型输入。请求失败或回执不完整时按 UNKNOWN 处理，不重试写。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

import httpx

from adapters.commerce.protocol import (
    ExecutionReceipt,
    ExecutionStatus,
    ReconciliationCommand,
    UpdatePriceCommand,
)
from runtime.execution.reconciliation import ReconcileResult


_MAX_RESPONSE_BYTES = 64 * 1024
_DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0)


@dataclass(frozen=True)
class _Response:
    status_code: int
    payload: dict[str, object] | None


class _ResponseTooLarge(ValueError):
    pass


class RestCommerceAdapter:
    """接入固定 REST 商品接口的 Adapter，不提供任意 API 调用能力。

    `base_url` 必须是 HTTPS 且 hostname 命中 `allowed_hosts`；`client` 所有权
    转交本实例，实例关闭时会一并关闭。测试可用 `httpx.MockTransport`。
    """

    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: frozenset[str],
        access_token: str,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        normalized_allowlist = {host.strip().lower() for host in allowed_hosts}
        if (
            parsed.scheme != "https"
            or hostname is None
            or hostname.lower() not in normalized_allowlist
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("commerce API endpoint must be an allowlisted HTTPS URL")
        if not access_token.strip():
            raise ValueError("commerce API access token must not be empty")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")

        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._client = client or httpx.AsyncClient(follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def execute(self, command: UpdatePriceCommand) -> ExecutionReceipt:
        path = (
            f"/stores/{quote(command.external_store_ref, safe='')}"
            f"/listings/{quote(command.external_product_ref, safe='')}/price"
        )
        response = await self._request(
            "PATCH",
            path,
            headers={"Idempotency-Key": command.idempotency_key},
            json_body={
                "price": str(command.target_price),
                "currency": command.currency,
                "expected_version": command.expected_version,
            },
        )
        if response is None:
            return self._unknown(command)
        if response.status_code == 409:
            return self._failed(command, "VERSION_CONFLICT")
        if response.status_code in (408, 425, 429) or response.status_code >= 500:
            return self._unknown(command)
        if not 200 <= response.status_code < 300:
            if 400 <= response.status_code < 500:
                return self._failed(command, "PLATFORM_REJECTED")
            return self._unknown(command)

        payload = response.payload
        if (
            payload is None
            or payload.get("status") != ExecutionStatus.APPLIED.value
            or payload.get("operation_id") != command.operation_id
        ):
            return self._unknown(command)
        try:
            before_price = Decimal(str(payload["before_price"]))
            after_price = Decimal(str(payload["after_price"]))
            version = payload["listing_version"]
            external_ref = payload["external_ref"]
            if (
                not before_price.is_finite()
                or not after_price.is_finite()
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 0
                or not isinstance(external_ref, str)
                or not external_ref
                or after_price != command.target_price
            ):
                return self._unknown(command)
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return self._unknown(command)
        return ExecutionReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=ExecutionStatus.APPLIED,
            before_price=before_price,
            after_price=after_price,
            listing_version=version,
            external_ref=external_ref,
        )

    async def reconcile(self, command: ReconciliationCommand) -> ReconcileResult:
        path = f"/operations/{quote(command.operation_id, safe='')}"
        response = await self._request("GET", path)
        if response is None:
            return ReconcileResult.UNKNOWN
        if response.status_code == 404:
            return ReconcileResult.NOT_APPLIED
        if not 200 <= response.status_code < 300 or response.payload is None:
            return ReconcileResult.UNKNOWN

        payload = response.payload
        if (
            payload.get("operation_id") != command.operation_id
            or payload.get("external_store_ref") != command.external_store_ref
            or payload.get("external_product_ref") != command.external_product_ref
            or payload.get("currency") != command.currency
        ):
            return ReconcileResult.UNKNOWN
        status = payload.get("status")
        if status == "NOT_APPLIED":
            return ReconcileResult.NOT_APPLIED
        if status != "APPLIED":
            return ReconcileResult.UNKNOWN
        try:
            price = Decimal(str(payload["price"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return ReconcileResult.UNKNOWN
        if not price.is_finite() or price != command.expected_price:
            return ReconcileResult.UNKNOWN
        return ReconcileResult.APPLIED

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> _Response | None:
        request_headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        try:
            async with self._client.stream(
                method,
                f"{self._base_url}{path}",
                headers=request_headers,
                json=json_body,
                timeout=self._timeout,
                follow_redirects=False,
            ) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > self._max_response_bytes:
                    return None
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_response_bytes:
                        raise _ResponseTooLarge
                try:
                    payload = json.loads(content) if content else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = None
                if payload is not None and not isinstance(payload, dict):
                    payload = None
                return _Response(response.status_code, payload)
        except (httpx.HTTPError, _ResponseTooLarge, ValueError):
            return None

    @staticmethod
    def _unknown(command: UpdatePriceCommand) -> ExecutionReceipt:
        return ExecutionReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=ExecutionStatus.UNKNOWN,
            reason_code="PLATFORM_OUTCOME_UNKNOWN",
        )

    @staticmethod
    def _failed(command: UpdatePriceCommand, reason_code: str) -> ExecutionReceipt:
        return ExecutionReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=ExecutionStatus.FAILED,
            reason_code=reason_code,
        )


__all__ = ["RestCommerceAdapter"]
