"""固定商品 REST API 契约的 CommerceAdapter 实现(P1 Task 6 迁移到 PricingGateway)。

该 Adapter 只允许受信任 HTTPS 主机上的商品价格读取/改价和操作回查端点;
URL 与 HTTP 方法不接受模型输入。请求失败或回执不完整时按 UNKNOWN 处理,
不重试写。

P1 起本 Adapter 实现 :class:`PricingGateway`(``get_price`` / ``set_price`` /
``reconcile_price``);旧 ``execute`` / ``reconcile`` 保留为兼容包装,委托新方法
并映射回旧回执类型,供历史 ToolVersion 的 Run 恢复。固定路径、安全主机与响应
大小限制与迁移前一致。

**能力范围(P1)**:本 Adapter 只声明 :data:`CAPABILITIES` 中的 ``pricing``。
库存与刊登能力不在它的适配边界内——真实第三方平台未必存在对应的可写端点,
**不允许**为了"凑齐接口"而猜测 URL。这两个能力的每个方法都必须立刻失败闭合:

- 读方法抛 :class:`CapabilityUnsupportedError`(``LookupError`` 家族,与 Port 的
  读失败通道一致);
- 写方法返回 ``FAILED`` + ``CAPABILITY_UNSUPPORTED`` 回执——写路径的确定失败本就
  以回执承载,不需要异常;它同时意味着"绝不重试",与 ``ProductOperationError``
  中 ``CAPABILITY_UNSUPPORTED`` 的语义(不得降级为删除资源等替代动作)一致;
- 回查返回 ``NOT_APPLIED``:能力不支持的写不可能生效,且回查不发起任何请求。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Final
from urllib.parse import quote, urlsplit

import httpx

from adapters.commerce.protocol import (
    ExecutionReceipt,
    ExecutionStatus,
    ReconciliationCommand,
    UpdatePriceCommand,
    is_retryable_reason_code,
)
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    InventoryLevelSnapshot,
    InventoryQuery,
    InventoryWriteReceipt,
    ReconcileInventoryCommand,
)
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingPublicationSnapshot,
    ListingQuery,
    ReconcileListingCommand,
)
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    PriceSnapshot,
    PriceUnavailable,
    PriceWriteReceipt,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.shared.errors import CAPABILITY_UNSUPPORTED
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.execution.reconciliation import ReconcileResult


CAPABILITIES: Final[frozenset[str]] = frozenset({"pricing"})
"""P1 的 REST Adapter 声明的能力集合:只有定价。"""


class CapabilityUnsupportedError(LookupError):
    """适配边界之外的业务能力被误调用(装配错误,不是平台故障)。

    ``LookupError`` 家族与 :class:`InventoryUnavailable` / :class:`ListingUnavailable`
    一致,因此读路径的失败通道不变;``reason_code`` 携带稳定的
    ``CAPABILITY_UNSUPPORTED`` 错误码,便于上层记录与诊断。
    """

    reason_code: Final = CAPABILITY_UNSUPPORTED

    def __init__(self, capability: str) -> None:
        super().__init__(
            f"{CAPABILITY_UNSUPPORTED}: REST adapter does not implement "
            f"the {capability!r} capability"
        )
        self.capability = capability


_MAX_RESPONSE_BYTES = 64 * 1024
_DEFAULT_TIMEOUT = httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0)
REASON_PLATFORM_OUTCOME_UNKNOWN = "PLATFORM_OUTCOME_UNKNOWN"
REASON_PLATFORM_REJECTED = "PLATFORM_REJECTED"


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

    # ---- 能力范围:P1 只声明定价 ----

    supported_capabilities: Final[frozenset[str]] = CAPABILITIES

    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot:
        """库存读取不在本 Adapter 能力内:立即失败,不猜测第三方端点。"""
        raise CapabilityUnsupportedError("inventory")

    async def adjust_inventory(
        self, command: AdjustInventoryCommand
    ) -> InventoryWriteReceipt:
        """库存写入返回确定失败回执:零副作用,且绝不重试。"""
        return InventoryWriteReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=WriteStatus.FAILED,
            reason_code=CAPABILITY_UNSUPPORTED,
        )

    async def reconcile_inventory(
        self, command: ReconcileInventoryCommand
    ) -> ReconcileResult:
        """能力不支持的写不可能生效:确定为 ``NOT_APPLIED``,且不发起任何请求。"""
        return ReconcileResult.NOT_APPLIED

    async def get_listing(self, query: ListingQuery) -> ListingPublicationSnapshot:
        """刊登读取不在本 Adapter 能力内:立即失败,不猜测第三方端点。"""
        raise CapabilityUnsupportedError("listing")

    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        """刊登发布返回确定失败回执:零副作用,且绝不重试。"""
        return _unsupported_receipt(command.operation_id, command.idempotency_key)

    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        """刊登取消发布返回确定失败回执:零副作用,且绝不重试。"""
        return _unsupported_receipt(command.operation_id, command.idempotency_key)

    async def reconcile_listing(
        self, command: ReconcileListingCommand
    ) -> ReconcileResult:
        """能力不支持的写不可能生效:确定为 ``NOT_APPLIED``,且不发起任何请求。"""
        return ReconcileResult.NOT_APPLIED

    # ---- PricingGateway ----

    async def get_price(self, query: PriceQuery) -> PriceSnapshot:
        """读取平台侧权威价格;非 2xx、响应不完整或币种不符即不可读。

        读失败不改状态,抛 :class:`PriceUnavailable`,调用方不得据此推断任何
        写入结果——与写回执的 ``UNKNOWN`` 语义严格区分。
        """
        path = (
            f"/stores/{quote(query.external_store_ref, safe='')}"
            f"/listings/{quote(query.external_product_ref, safe='')}/price"
        )
        response = await self._request("GET", path)
        if response is None or not 200 <= response.status_code < 300:
            raise PriceUnavailable("platform price read failed")
        payload = response.payload
        if payload is None:
            raise PriceUnavailable("platform price payload is incomplete")
        external_ref = payload.get("external_ref")
        if (
            payload.get("currency") != query.currency
            or not isinstance(external_ref, str)
            or not external_ref
        ):
            raise PriceUnavailable("platform price payload is incomplete")
        try:
            price = Decimal(str(payload["price"]))
            version = payload["listing_version"]
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise PriceUnavailable("platform price payload is invalid") from error
        if (
            not price.is_finite()
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 0
        ):
            raise PriceUnavailable("platform price payload is invalid")
        return PriceSnapshot(
            price=price,
            currency=query.currency,
            listing_version=version,
            external_ref=external_ref,
        )

    async def set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        """执行绝对目标价写入;超时或响应丢失返回 UNKNOWN,由调用方回查。

        ``Idempotency-Key`` 沿用 Runtime 注入的 ``command.idempotency_key``。
        """
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
            return _unknown(command)
        if response.status_code == 409:
            return _failed(command, "VERSION_CONFLICT")
        if response.status_code in (408, 425, 429) or response.status_code >= 500:
            return _unknown(command)
        if not 200 <= response.status_code < 300:
            if 400 <= response.status_code < 500:
                return _failed(command, REASON_PLATFORM_REJECTED)
            return _unknown(command)

        payload = response.payload
        if (
            payload is None
            or payload.get("status") != ExecutionStatus.APPLIED.value
            or payload.get("operation_id") != command.operation_id
        ):
            return _unknown(command)
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
                return _unknown(command)
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return _unknown(command)
        return PriceWriteReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=WriteStatus.APPLIED,
            before_price=before_price,
            after_price=after_price,
            resource_version=version,
            external_ref=external_ref,
            changed=True,
        )

    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        """回查改价是否生效;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。"""
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

    # ---- 兼容包装(历史 ToolVersion 的 Run 恢复) ----

    async def execute(self, command: UpdatePriceCommand) -> ExecutionReceipt:
        """旧写入口:委托 :meth:`set_price` 并映射回 :class:`ExecutionReceipt`。"""
        receipt = await self.set_price(_to_set_price_command(command))
        return _to_execution_receipt(receipt, command.idempotency_key)

    async def reconcile(self, command: ReconciliationCommand) -> ReconcileResult:
        """旧回查入口:委托 :meth:`reconcile_price`。"""
        return await self.reconcile_price(
            ReconcilePriceCommand(
                operation_id=command.operation_id,
                workspace_id=command.workspace_id,
                store_id=command.store_id,
                listing_id=command.listing_id,
                external_store_ref=command.external_store_ref,
                external_product_ref=command.external_product_ref,
                expected_price=command.expected_price,
                currency=command.currency,
            )
        )

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


def _unknown(command: SetListingPriceCommand) -> PriceWriteReceipt:
    """无观测的 UNKNOWN 回执:只能交给回查,不得携带任何状态。"""
    return PriceWriteReceipt(
        operation_id=command.operation_id,
        idempotency_key=command.idempotency_key,
        status=WriteStatus.UNKNOWN,
        reason_code=REASON_PLATFORM_OUTCOME_UNKNOWN,
    )


def _failed(command: SetListingPriceCommand, reason_code: str) -> PriceWriteReceipt:
    """确定失败回执;状态与原因码一一对应,不带观测。"""
    return PriceWriteReceipt(
        operation_id=command.operation_id,
        idempotency_key=command.idempotency_key,
        status=WriteStatus.FAILED,
        reason_code=reason_code,
    )


def _unsupported_receipt(operation_id: str, idempotency_key: str) -> WriteReceipt:
    """能力不支持的刊登写回执:确定失败、零观测,只能落 ToolCall 终态。"""
    return WriteReceipt(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        status=WriteStatus.FAILED,
        reason_code=CAPABILITY_UNSUPPORTED,
    )


def _to_set_price_command(command: UpdatePriceCommand) -> SetListingPriceCommand:
    """旧写命令 → PricingGateway 写命令(幂等键沿用旧命令派生)。"""
    return SetListingPriceCommand(
        operation_id=command.operation_id,
        idempotency_key=command.idempotency_key,
        workspace_id=command.workspace_id,
        store_id=command.store_id,
        listing_id=command.listing_id,
        external_store_ref=command.external_store_ref,
        external_product_ref=command.external_product_ref,
        sku="",
        target_price=command.target_price,
        currency=command.currency,
        expected_version=command.expected_version,
    )


def _to_execution_receipt(
    receipt: PriceWriteReceipt, idempotency_key: str
) -> ExecutionReceipt:
    """价格回执 → 旧 :class:`ExecutionReceipt`(幂等键沿用旧命令派生)。"""
    return ExecutionReceipt(
        operation_id=receipt.operation_id,
        idempotency_key=idempotency_key,
        status=ExecutionStatus(receipt.status.value),
        before_price=receipt.before_price,
        after_price=receipt.after_price,
        listing_version=receipt.resource_version,
        external_ref=receipt.external_ref,
        reason_code=receipt.reason_code,
        retryable=is_retryable_reason_code(receipt.reason_code),
    )


if TYPE_CHECKING:
    from agents.product_operations.inventory.gateway import InventoryGateway
    from agents.product_operations.listing.gateway import ListingGateway
    from agents.product_operations.pricing.gateway import PricingGateway

    # 声明能力范围不等于"接口形状可以不合":三个 Port 的方法签名仍必须吻合,
    # 否则组合根在把本 Adapter 当作某能力注入时不会有任何静态提示。
    _pricing_conformance: type[PricingGateway] = RestCommerceAdapter
    _inventory_conformance: type[InventoryGateway] = RestCommerceAdapter
    _listing_conformance: type[ListingGateway] = RestCommerceAdapter


__all__ = ["CAPABILITIES", "CapabilityUnsupportedError", "RestCommerceAdapter"]
