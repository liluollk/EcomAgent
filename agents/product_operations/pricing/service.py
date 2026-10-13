"""Pricing 应用服务与写 Handler(P1 Task 6;设计 §4.3、§5.2、§6)。

- :class:`PricingService` 提供确定性价格比较:经授权的
  :class:`StoreReferenceResolver` 解析 ``store_refs`` 后,只比较**已发布**
  Listing,同币种给出最低价/最高价/价差,多币种直接以 ``CURRENCY_MISMATCH``
  拒绝,绝不代模型换算;
- :class:`SetListingPriceHandler` 实现 Runtime 两阶段写协议:``prepare`` 在
  无外部副作用的前提下解析可信资源、读取版本、执行 PricingPolicy 执行校验并
  构造可回放的 ``expected_state``;``execute_prepared`` 只把该命令交给注入的
  :class:`PricingGateway`,不接受模型参数,异常交由 ToolExecutor 归 UNKNOWN。
  ``prepare`` 失败返回**类型化拒绝**:保留真实规范错误码(NOT_FOUND /
  AMBIGUOUS_STORE_REFERENCE / RESOURCE_SCOPE_DENIED / LISTING_NOT_FOUND /
  VERSION_CONFLICT / VALIDATION_FAILED + rule_code),不把越权与规则拒绝统一
  伪装成"资源不存在"。

导入方向:本模块不 import adapters(平台桥接由组合根完成)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus, StoreStatus
from agents.product_operations.input.normalizer import normalize_arguments
from agents.product_operations.input.profiles import p1_fields_for
from agents.product_operations.input.references import StoreReferenceResolver
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.policy.models import PricePolicyConfig
from agents.product_operations.policy.price_policy import (
    LISTING_NOT_FOUND as POLICY_LISTING_NOT_FOUND,
)
from agents.product_operations.policy.price_policy import (
    LISTING_VERSION_CONFLICT as POLICY_LISTING_VERSION_CONFLICT,
)
from agents.product_operations.pricing.gateway import (
    PriceWriteReceipt,
    PricingGateway,
    SetListingPriceCommand,
)
from agents.product_operations.pricing.models import (
    CURRENCY_MISMATCH,
    NO_PUBLISHED_LISTING,
    ListingPriceRow,
    PriceComparisonSummary,
    SetListingPriceResult,
)
from agents.product_operations.pricing.policy import PricingPolicy
from agents.product_operations.shared.errors import (
    VALIDATION_FAILED,
    VERSION_CONFLICT,
    ProductOperationError,
)
from agents.product_operations.shared.write_contracts import WriteStatus
from agents.product_operations.tools.access import authorize_product_read
from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext
from runtime.execution.tool_executor import PreparedToolWrite, PreparedWriteRejection
from runtime.execution.tool_result_processor import ToolExecutionReceipt


STABLE_KEY_SET_LISTING_PRICE: Final = "set_listing_price"
SET_LISTING_PRICE_VERSION: Final = 1
PRODUCT_PRICE_WRITE_PERMISSION: Final = "product.price.write"

OPERATION_TYPE: Final = "SET_LISTING_PRICE"
"""本能力的受控 ``operation_type``;模型不可见、不可填写。

它由已固定的 ToolVersion 在 prepare 阶段写入 ``expected_state``,恢复时据其
分发到 :class:`~agents.product_operations.pricing.reconciliation.PriceReconciler`。
"""

LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
"""listing 在 (workspace, store, sku) 下确实不存在时的规范错误码。"""

INVALID_ARGUMENTS: Final = "INVALID_ARGUMENTS"
"""参数形状非法时的规则码(作 ``VALIDATION_FAILED`` 的 ``rule_code``)。"""

_POLICY_REASON_TO_ERROR_CODE: Final[dict[str, str]] = {
    # 策略层的具体原因码 → 规范的 error_code;未列出的规则违规一律归
    # ``VALIDATION_FAILED``,具体规则保留在 ``rule_code``,不伪造成"资源不存在"。
    POLICY_LISTING_NOT_FOUND: LISTING_NOT_FOUND,
    POLICY_LISTING_VERSION_CONFLICT: VERSION_CONFLICT,
}


class PricingService:
    """价格只读应用服务:确定性价格比较。"""

    def __init__(
        self, session: AsyncSession, *, stores: StoreReferenceResolver | None = None
    ) -> None:
        self._session = session
        self._stores = stores or StoreReferenceResolver(session)

    async def compare_listing_prices(
        self,
        *,
        actor: ActorContext,
        sku: str,
        store_refs: Sequence[str] = (),
    ) -> PriceComparisonSummary:
        """比较授权店铺内该 SKU 已发布 Listing 的价格。

        ``store_refs`` 为空表示全部授权店铺;无授权店铺、无已发布 Listing 或
        多币种都以规范错误码显式失败,不静默收窄。
        """
        authorize_product_read(actor)
        store_ids = await self._stores.resolve_authorized_many(actor, store_refs)
        if not store_ids:
            raise ProductOperationError(
                NO_PUBLISHED_LISTING, "当前授权范围内没有可比较的店铺"
            )
        stmt = (
            select(Store, Listing)
            .join_from(Listing, Store, Store.id == Listing.store_id)
            .join_from(Listing, Product, Product.id == Listing.product_id)
            .where(
                Listing.workspace_id == actor.workspace_id,
                Product.workspace_id == actor.workspace_id,
                Store.workspace_id == actor.workspace_id,
                Listing.status == ListingStatus.PUBLISHED,
                Store.status == StoreStatus.ACTIVE,
                Listing.store_id.in_(sorted(store_ids)),
                Product.sku == sku,
            )
            .order_by(Listing.current_price, Store.name, Store.id)
        )
        pairs = (await self._session.execute(stmt)).all()
        if not pairs:
            raise ProductOperationError(
                NO_PUBLISHED_LISTING,
                f"授权店铺内不存在 SKU {sku!r} 的已发布 Listing",
            )
        currencies = {listing.currency for _, listing in pairs}
        if len(currencies) > 1:
            raise ProductOperationError(
                CURRENCY_MISMATCH,
                f"SKU {sku!r} 的已发布 Listing 存在多个币种,禁止直接比较",
            )
        rows = tuple(
            ListingPriceRow(
                store_ref=store.name,
                price=listing.current_price,
                currency=listing.currency,
            )
            for store, listing in pairs
        )
        prices = [row.price for row in rows]
        return PriceComparisonSummary(
            sku=sku,
            currency=next(iter(currencies)),
            rows=rows,
            lowest_price=min(prices),
            highest_price=max(prices),
            price_spread=max(prices) - min(prices),
        )


@dataclass(frozen=True)
class PreparedSetPrice:
    """prepare 阶段解析出的可信资源与目标价(不含 ``operation_id``)。"""

    workspace_id: uuid.UUID
    store_ref: str
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    sku: str
    target_price: Decimal
    currency: str
    expected_version: int
    policy_version: str

    def expected_state(self) -> dict[str, Any]:
        """可回放的业务预期(服务端补全,模型不可见)。"""
        return {
            "operation_type": OPERATION_TYPE,
            "workspace_id": str(self.workspace_id),
            "store_id": str(self.store_id),
            "listing_id": str(self.listing_id),
            "store_ref": self.store_ref,
            "sku": self.sku,
            "target_price": str(self.target_price),
            "currency": self.currency,
            "expected_version": self.expected_version,
            "policy_version": self.policy_version,
            "external_store_ref": self.external_store_ref,
            "external_product_ref": self.external_product_ref,
        }

    def to_command(self, operation_id: uuid.UUID, idempotency_key: str) -> SetListingPriceCommand:
        """注入执行边界分配的 ``operation_id`` 与幂等键,得到平台写命令。"""
        return SetListingPriceCommand(
            operation_id=str(operation_id),
            idempotency_key=idempotency_key,
            workspace_id=self.workspace_id,
            store_id=self.store_id,
            listing_id=self.listing_id,
            external_store_ref=self.external_store_ref,
            external_product_ref=self.external_product_ref,
            sku=self.sku,
            target_price=self.target_price,
            currency=self.currency,
            expected_version=self.expected_version,
        )


@dataclass(frozen=True)
class _Rejection:
    """prepare 阶段无法构造可信写命令时的类型化拒绝。

    ``error_code`` 是规范错误码(NOT_FOUND / AMBIGUOUS_STORE_REFERENCE /
    RESOURCE_SCOPE_DENIED / LISTING_NOT_FOUND / VERSION_CONFLICT /
    VALIDATION_FAILED),``rule_code`` 是更具体的规则或原因(如价格下限、
    币种不符、参数形状非法),``detail`` 是安全诊断文案。三者都进审计上下文:
    把越权、规则拒绝统一伪装成"资源不存在"会破坏安全归因与模型纠错。
    """

    error_code: str
    rule_code: str | None
    detail: str


class SetListingPriceHandler:
    """``set_listing_price`` 的外部执行端点(Runtime ``WriteToolHandler``)。

    绑定请求级 Actor 与 Session:``store_ref`` 是模型可见引用,必须经授权的
    :class:`StoreReferenceResolver` 解析;权限、资源范围与 ExecutionMode 已由
    PreToolUse 完成,``prepare`` 只做服务端补全与最终业务校验(设计 §5.2)。
    """

    def __init__(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        gateway: PricingGateway,
        policy_config: PricePolicyConfig,
        stores: StoreReferenceResolver | None = None,
    ) -> None:
        self._actor = actor
        self._workspace_id = actor.workspace_id
        self._session = session
        self._gateway = gateway
        self._policy = PricingPolicy(session, policy_config)
        self._stores = stores or StoreReferenceResolver(session)

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        """解析引用 → 读取版本 → 执行校验 → 可信命令 + ``expected_state``。

        任一环节失败都返回**类型化**拒绝:保留真实 ``error_code``(资源不存在、
        越权、版本冲突、规则校验失败)与其具体规则码,不统一伪装成"Listing 不存在"。
        """
        arguments = normalize_arguments(
            dict(normalized_arguments), fields=p1_fields_for(STABLE_KEY_SET_LISTING_PRICE)
        )
        try:
            resolved = await self._resolve(arguments)
        except BaseException:
            await self._session.rollback()
            raise
        # Listing 读取事务到此结束:平台 Adapter 自行开启并提交它的写事务。
        await self._session.commit()
        if isinstance(resolved, _Rejection):
            context = _rejection_context(arguments, self._policy.policy_version, resolved)
            return PreparedToolWrite(
                command=None,
                expected_state=None,
                request_fingerprint=_fingerprint(context),
                rejection=PreparedWriteRejection(
                    status="FAILED",
                    error_code=resolved.error_code,
                    context=context,
                ),
            )
        expected_state = resolved.expected_state()
        return PreparedToolWrite(
            command=resolved,
            expected_state=expected_state,
            request_fingerprint=_fingerprint(expected_state),
        )

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: PreparedToolWrite,
    ) -> ToolExecutionReceipt:
        """只用 prepare 结果写平台;异常交由 ToolExecutor 归 UNKNOWN。

        幂等键由 Runtime 的执行边界注入,原样透传给平台命令;模型参数不再
        参与,目标价在 prepare 阶段已固定。
        """
        prepared_price = cast("PreparedSetPrice", prepared.command)
        command = prepared_price.to_command(operation_id, idempotency_key)
        receipt = await self._gateway.set_price(command)
        return self._to_tool_receipt(tool_call_id, prepared_price, receipt)

    async def _resolve(self, arguments: Mapping[str, Any]) -> PreparedSetPrice | _Rejection:
        """服务端补全:解析 ``store_ref`` 与 Listing,执行 PricingPolicy 校验。

        返回可信命令或类型化拒绝,不把不同失败原因折叠成同一个 error_code。
        """
        store_ref = arguments.get("store_ref")
        sku = arguments.get("sku")
        target_price = arguments.get("target_price")
        if not isinstance(store_ref, str) or not isinstance(sku, str):
            return _Rejection(
                VALIDATION_FAILED, INVALID_ARGUMENTS, "store_ref/sku 必须是字符串"
            )
        if not isinstance(target_price, Decimal):
            return _Rejection(
                VALIDATION_FAILED, INVALID_ARGUMENTS, "target_price 必须是十进制金额"
            )
        store_id, store_rejection = await self._resolve_store(store_ref)
        if store_rejection is not None:
            return store_rejection
        assert store_id is not None
        listing_and_store = await self._resolve_listing(store_id, sku)
        if listing_and_store is None:
            return _Rejection(
                LISTING_NOT_FOUND,
                None,
                f"店铺内不存在 SKU {sku!r} 的 Listing",
            )
        listing, store = listing_and_store
        decision = await self._policy.validate_execution(
            actor=self._actor,
            store_id=store_id,
            sku=sku,
            target_price=target_price,
            expected_version=listing.version,
        )
        if not decision.allowed:
            reason = decision.reason_code or VALIDATION_FAILED
            return _Rejection(
                _POLICY_REASON_TO_ERROR_CODE.get(reason, VALIDATION_FAILED),
                reason,
                decision.safe_message or reason,
            )
        return PreparedSetPrice(
            workspace_id=self._workspace_id,
            store_ref=store_ref,
            store_id=store_id,
            listing_id=listing.id,
            external_store_ref=store.external_store_ref,
            external_product_ref=listing.external_product_ref,
            sku=sku,
            target_price=target_price,
            currency=listing.currency,
            expected_version=listing.version,
            policy_version=self._policy.policy_version,
        )

    async def _resolve_store(self, store_ref: str) -> tuple[uuid.UUID | None, _Rejection | None]:
        """解析模型可见引用,保留真实拒绝原因(无匹配/歧义/越权)。

        错误通道固定两类:授权与资源范围拒绝用 ``ToolAccessError``(规范码
        ``RESOURCE_SCOPE_DENIED``);领域查找失败用 ``ProductOperationError``
        (``NOT_FOUND`` / ``AMBIGUOUS_STORE_REFERENCE``)。两者都不折叠成
        ``LISTING_NOT_FOUND``。
        """
        try:
            store_id = await self._stores.resolve_authorized(self._actor, store_ref)
        except ProductOperationError as error:
            return None, _Rejection(error.code, None, error.detail or error.code)
        except ToolAccessError as error:
            return None, _Rejection(error.reason_code, None, str(error))
        return store_id, None

    async def _resolve_listing(
        self, store_id: uuid.UUID, sku: str
    ) -> tuple[Listing, Store] | None:
        stmt = (
            select(Listing, Store)
            .join(Product, Listing.product_id == Product.id)
            .join(Store, Listing.store_id == Store.id)
            .where(
                Listing.workspace_id == self._workspace_id,
                Listing.store_id == store_id,
                Product.sku == sku,
            )
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).one_or_none()

    def _to_tool_receipt(
        self,
        tool_call_id: uuid.UUID,
        prepared: PreparedSetPrice,
        receipt: PriceWriteReceipt,
    ) -> ToolExecutionReceipt:
        """写回执 → Runtime ``ToolExecutionReceipt``(payload 对齐 Output Schema)。"""
        payload: dict[str, Any] | None = None
        if receipt.status is WriteStatus.APPLIED:
            result = SetListingPriceResult(
                operation_id=receipt.operation_id,
                policy_version=prepared.policy_version,
                store_ref=prepared.store_ref,
                sku=prepared.sku,
                currency=prepared.currency,
                target_price=prepared.target_price,
                listing_version=receipt.resource_version,
                external_ref=receipt.external_ref,
            )
            payload = result.model_dump(mode="json", exclude_none=True)
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            status=receipt.status.value,
            payload=payload,
            external_reference=receipt.external_ref,
            error_code=receipt.reason_code,
        )


def _fingerprint(expected_state: Mapping[str, Any]) -> str:
    """``expected_state`` 的规范化 JSON 指纹(随 Attempt 留痕)。"""
    canonical = json.dumps(expected_state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rejection_context(
    arguments: Mapping[str, Any], policy_version: str, rejection: _Rejection
) -> dict[str, str]:
    """无法构造可信目标时的最小审计上下文,不臆测 Listing 侧字段。

    额外保留类型化拒绝的 ``rejection_detail`` 与具体 ``rule_code``(存在时),
    使安全审计与评测能看到真实原因,而不是统一的"资源不存在"。
    """
    context: dict[str, str] = {
        "operation_type": OPERATION_TYPE,
        "store_ref": str(arguments.get("store_ref", "")),
        "sku": str(arguments.get("sku", "")),
        "target_price": str(arguments.get("target_price", "")),
        "policy_version": policy_version,
        "rejection_detail": rejection.detail,
    }
    if rejection.rule_code is not None:
        context["rule_code"] = rejection.rule_code
    return context


__all__ = [
    "INVALID_ARGUMENTS",
    "LISTING_NOT_FOUND",
    "OPERATION_TYPE",
    "PRODUCT_PRICE_WRITE_PERMISSION",
    "SET_LISTING_PRICE_VERSION",
    "STABLE_KEY_SET_LISTING_PRICE",
    "PreparedSetPrice",
    "PricingService",
    "SetListingPriceHandler",
]
