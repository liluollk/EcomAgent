"""Inventory 应用服务与写 Handler(P1 Task 7;设计 §4.4、§5.2、§6)。

- :class:`InventoryService` 提供确定性的库存读取:经授权的
  :class:`StoreReferenceResolver` 解析 ``store_ref`` 后,把 ``location_ref``
  解析为店铺内**唯一确定**的履约位置(未给出位置且店铺仅有唯一启用位置时自动
  解析,存在多个位置时以 ``AMBIGUOUS_LOCATION`` 要求用户补充),再读取
  (位置, 商品) 的权威库存;
- :class:`AdjustInventoryQuantityHandler` 实现 Runtime 两阶段写协议:``prepare``
  在无外部副作用的前提下解析可信资源、读取库存版本、执行
  :class:`InventoryPolicy` 执行校验并构造可回放的 ``expected_state``
  (``quantity_before`` / ``delta`` / ``expected_quantity_after`` /
  ``expected_version`` / 位置 / 原因);``execute_prepared`` 只把该命令交给注入的
  :class:`InventoryGateway`,不接受模型参数,异常交由 ToolExecutor 归 UNKNOWN。
  ``prepare`` 失败返回**类型化拒绝**:保留真实规范错误码(NOT_FOUND /
  AMBIGUOUS_STORE_REFERENCE / AMBIGUOUS_LOCATION / RESOURCE_SCOPE_DENIED /
  INVENTORY_NOT_FOUND / VERSION_CONFLICT / VALIDATION_FAILED + rule_code),
  不把越权与规则拒绝统一伪装成"资源不存在"。

导入方向:本模块不 import adapters(平台桥接由组合根完成)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StoreStatus
from agents.product_operations.input.normalizer import normalize_arguments
from agents.product_operations.input.profiles import p1_fields_for
from agents.product_operations.input.references import StoreReferenceResolver
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    InventoryGateway,
    InventoryWriteReceipt,
)
from agents.product_operations.inventory.models import (
    AdjustInventoryQuantityResult,
    AmbiguousLocationError,
    InventoryLevelResult,
)
from agents.product_operations.inventory.policy import (
    INVENTORY_VERSION_CONFLICT as POLICY_INVENTORY_VERSION_CONFLICT,
)
from agents.product_operations.inventory.policy import InventoryPolicy, InventoryPolicyConfig
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    Listing,
    Product,
    Store,
)
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.shared.errors import (
    AMBIGUOUS_LOCATION,
    NOT_FOUND,
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


STABLE_KEY_ADJUST_INVENTORY_QUANTITY: Final = "adjust_inventory_quantity"
ADJUST_INVENTORY_QUANTITY_VERSION: Final = 1
PRODUCT_INVENTORY_WRITE_PERMISSION: Final = "product.inventory.write"

OPERATION_TYPE: Final = "ADJUST_INVENTORY"
"""本能力的受控 ``operation_type``;模型不可见、不可填写。

它由已固定的 ToolVersion 在 prepare 阶段写入 ``expected_state``,恢复时据其
分发到 :class:`~agents.product_operations.inventory.reconciliation.InventoryReconciler`。
"""

INVENTORY_NOT_FOUND: Final = "INVENTORY_NOT_FOUND"
"""该 (位置, 商品) 确实不存在库存记录时的规范错误码。"""

INVALID_ARGUMENTS: Final = "INVALID_ARGUMENTS"
"""参数形状非法时的规则码(作 ``VALIDATION_FAILED`` 的 ``rule_code``)。"""

_POLICY_REASON_TO_ERROR_CODE: Final[dict[str, str]] = {
    # 策略层的具体原因码 → 规范的 error_code;未列出的规则违规一律归
    # ``VALIDATION_FAILED``,具体规则保留在 ``rule_code``,不伪造成"资源不存在"。
    POLICY_INVENTORY_VERSION_CONFLICT: VERSION_CONFLICT,
}


async def resolve_location_for_store(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    store_id: uuid.UUID,
    location_ref: str | None,
) -> FulfillmentLocation:
    """把 ``location_ref`` 解析为店铺内唯一确定的启用履约位置。

    - 候选为 ``(workspace, store)`` 下全部 ACTIVE 位置;
    - ``location_ref`` 未给出:唯一位置自动解析;无位置 → ``NOT_FOUND``;
      多个位置 → :class:`AmbiguousLocationError`(需用户补充);
    - ``location_ref`` 已给出:按名称/别名匹配,零匹配 → ``NOT_FOUND``,
      多匹配 → :class:`AmbiguousLocationError`。
    """
    candidates = (
        await session.scalars(
            select(FulfillmentLocation)
            .where(
                FulfillmentLocation.workspace_id == workspace_id,
                FulfillmentLocation.store_id == store_id,
                FulfillmentLocation.status == StoreStatus.ACTIVE,
            )
            .order_by(FulfillmentLocation.name, FulfillmentLocation.id)
        )
    ).all()
    if location_ref is None:
        if not candidates:
            raise ProductOperationError(NOT_FOUND, "店铺内没有启用中的履约位置")
        if len(candidates) > 1:
            raise AmbiguousLocationError(
                f"店铺内存在 {len(candidates)} 个启用位置,未指定 location_ref"
            )
        return candidates[0]
    normalized = location_ref.strip()
    if not normalized:
        raise ProductOperationError(NOT_FOUND, "location_ref 为空,无法解析位置")
    matches = [
        candidate
        for candidate in candidates
        if normalized == candidate.name or normalized in candidate.aliases
    ]
    if not matches:
        raise ProductOperationError(
            NOT_FOUND, f"位置引用 {location_ref!r} 在该店铺无匹配"
        )
    if len(matches) > 1:
        raise AmbiguousLocationError(
            f"位置引用 {location_ref!r} 匹配到 {len(matches)} 个位置,无法确定性解析"
        )
    return matches[0]


class InventoryService:
    """库存只读应用服务:位置解析 + 权威库存读取。"""

    def __init__(
        self, session: AsyncSession, *, stores: StoreReferenceResolver | None = None
    ) -> None:
        self._session = session
        self._stores = stores or StoreReferenceResolver(session)

    async def get_inventory_level(
        self,
        *,
        actor: ActorContext,
        store_ref: str,
        sku: str,
        location_ref: str | None = None,
    ) -> InventoryLevelResult:
        """读取授权店铺内指定 SKU 在解析后位置的权威库存。

        店铺引用无法解析 → ``NOT_FOUND`` / ``AMBIGUOUS_STORE_REFERENCE``;
        越权店铺 → ``RESOURCE_SCOPE_DENIED``;店铺内无该 SKU 的 Listing 或
        (位置, 商品) 的库存记录 → ``NOT_FOUND`` / ``INVENTORY_NOT_FOUND``。
        """
        authorize_product_read(actor)
        store_id = await self._stores.resolve_authorized(actor, store_ref)
        location = await resolve_location_for_store(
            self._session, actor.workspace_id, store_id, location_ref
        )
        listing = await _resolve_listing(self._session, actor.workspace_id, store_id, sku)
        if listing is None:
            raise ProductOperationError(
                NOT_FOUND, f"店铺 {store_ref!r} 中不存在 SKU {sku!r} 的 Listing"
            )
        level = await ProductRepository(self._session).get_inventory_level(
            actor.workspace_id, location.id, listing.product_id
        )
        if level is None:
            raise ProductOperationError(
                INVENTORY_NOT_FOUND,
                f"位置 {location.name!r} 内不存在 SKU {sku!r} 的库存记录",
            )
        return InventoryLevelResult(
            store_ref=store_ref,
            sku=sku,
            location_ref=location.name,
            available_quantity=level.available_quantity,
            inventory_version=level.version,
        )


async def _resolve_listing(
    session: AsyncSession, workspace_id: uuid.UUID, store_id: uuid.UUID, sku: str
) -> Listing | None:
    """按 ``(workspace, store, sku)`` 读取 Listing 的确定性查询。"""
    stmt = (
        select(Listing)
        .join(Product, Listing.product_id == Product.id)
        .where(
            Listing.workspace_id == workspace_id,
            Listing.store_id == store_id,
            Product.workspace_id == workspace_id,
            Product.sku == sku,
        )
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one_or_none()


@dataclass(frozen=True)
class PreparedAdjustInventory:
    """prepare 阶段解析出的可信资源与目标增量(不含 ``operation_id``)。"""

    workspace_id: uuid.UUID
    store_ref: str
    store_id: uuid.UUID
    location_ref: str
    location_id: uuid.UUID
    product_id: uuid.UUID
    external_store_ref: str
    external_location_ref: str
    external_product_ref: str
    sku: str
    delta: int
    reason: str
    quantity_before: int
    expected_version: int
    policy_version: str

    def expected_state(self) -> dict[str, Any]:
        """可回放的业务预期(服务端补全,模型不可见)。"""
        return {
            "operation_type": OPERATION_TYPE,
            "workspace_id": str(self.workspace_id),
            "store_id": str(self.store_id),
            "location_id": str(self.location_id),
            "product_id": str(self.product_id),
            "store_ref": self.store_ref,
            "location_ref": self.location_ref,
            "sku": self.sku,
            "delta": self.delta,
            "reason": self.reason,
            "quantity_before": self.quantity_before,
            "expected_quantity_after": self.quantity_before + self.delta,
            "expected_version": self.expected_version,
            "policy_version": self.policy_version,
            "external_store_ref": self.external_store_ref,
            "external_location_ref": self.external_location_ref,
            "external_product_ref": self.external_product_ref,
        }

    def to_command(
        self, operation_id: uuid.UUID, idempotency_key: str
    ) -> AdjustInventoryCommand:
        """注入执行边界分配的 ``operation_id`` 与幂等键,得到平台写命令。"""
        return AdjustInventoryCommand(
            operation_id=str(operation_id),
            idempotency_key=idempotency_key,
            workspace_id=self.workspace_id,
            store_id=self.store_id,
            location_id=self.location_id,
            product_id=self.product_id,
            external_store_ref=self.external_store_ref,
            external_location_ref=self.external_location_ref,
            external_product_ref=self.external_product_ref,
            sku=self.sku,
            delta=self.delta,
            reason=self.reason,
            expected_version=self.expected_version,
            quantity_before=self.quantity_before,
        )


@dataclass(frozen=True)
class _Rejection:
    """prepare 阶段无法构造可信写命令时的类型化拒绝。

    ``error_code`` 是规范错误码(NOT_FOUND / AMBIGUOUS_STORE_REFERENCE /
    AMBIGUOUS_LOCATION / RESOURCE_SCOPE_DENIED / INVENTORY_NOT_FOUND /
    VERSION_CONFLICT / VALIDATION_FAILED),``rule_code`` 是更具体的规则
    (如 delta 非法、数量为负),``detail`` 是安全诊断文案。三者都进审计上下文:
    把越权、规则拒绝统一伪装成"资源不存在"会破坏安全归因与模型纠错。
    """

    error_code: str
    rule_code: str | None
    detail: str


class AdjustInventoryQuantityHandler:
    """``adjust_inventory_quantity`` 的外部执行端点(Runtime ``WriteToolHandler``)。

    绑定请求级 Actor 与 Session:``store_ref``/``location_ref`` 是模型可见引用,
    必须经授权的 :class:`StoreReferenceResolver` 与位置解析器解析;权限、资源范围
    与 ExecutionMode 已由 PreToolUse 完成,``prepare`` 只做服务端补全与最终业务
    校验(设计 §5.2)。
    """

    def __init__(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        gateway: InventoryGateway,
        policy_config: InventoryPolicyConfig,
        stores: StoreReferenceResolver | None = None,
    ) -> None:
        self._actor = actor
        self._workspace_id = actor.workspace_id
        self._session = session
        self._gateway = gateway
        self._policy = InventoryPolicy(session, policy_config)
        self._stores = stores or StoreReferenceResolver(session)

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        """解析引用 → 读取库存版本 → 执行校验 → 可信命令 + ``expected_state``。

        任一环节失败都返回**类型化**拒绝:保留真实 ``error_code``(资源不存在、
        歧义位置、越权、版本冲突、规则校验失败)与其具体规则码,不统一伪装成
        "库存不存在"。
        """
        arguments = normalize_arguments(
            dict(normalized_arguments),
            fields=p1_fields_for(STABLE_KEY_ADJUST_INVENTORY_QUANTITY),
        )
        try:
            resolved = await self._resolve(arguments)
        except BaseException:
            await self._session.rollback()
            raise
        # 库存读取事务到此结束:平台 Adapter 自行开启并提交它的写事务。
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
        """只用 prepare 结果写平台;异常交由 ToolExecutor 归 UNKNOWN。"""
        prepared_inventory = cast("PreparedAdjustInventory", prepared.command)
        command = prepared_inventory.to_command(operation_id, idempotency_key)
        receipt = await self._gateway.adjust_inventory(command)
        return self._to_tool_receipt(tool_call_id, prepared_inventory, receipt)

    async def _resolve(
        self, arguments: Mapping[str, Any]
    ) -> PreparedAdjustInventory | _Rejection:
        """服务端补全:解析店铺/位置/Listing,执行 InventoryPolicy 校验。"""
        store_ref = arguments.get("store_ref")
        sku = arguments.get("sku")
        location_ref = arguments.get("location_ref")
        delta = arguments.get("delta")
        reason = arguments.get("reason")
        if not isinstance(store_ref, str) or not isinstance(sku, str):
            return _Rejection(
                VALIDATION_FAILED, INVALID_ARGUMENTS, "store_ref/sku 必须是字符串"
            )
        if location_ref is not None and not isinstance(location_ref, str):
            return _Rejection(
                VALIDATION_FAILED, INVALID_ARGUMENTS, "location_ref 必须是字符串或省略"
            )
        if not isinstance(delta, int) or isinstance(delta, bool):
            return _Rejection(VALIDATION_FAILED, INVALID_ARGUMENTS, "delta 必须是整数")
        reason_value = getattr(reason, "value", reason)
        if not isinstance(reason_value, str):
            return _Rejection(VALIDATION_FAILED, INVALID_ARGUMENTS, "reason 必须是受控枚举")

        store_id, store_rejection = await self._resolve_store(store_ref)
        if store_rejection is not None:
            return store_rejection
        assert store_id is not None
        listing = await _resolve_listing(self._session, self._workspace_id, store_id, sku)
        if listing is None:
            return _Rejection(
                NOT_FOUND, None, f"店铺内不存在 SKU {sku!r} 的 Listing"
            )
        store = (
            await self._session.scalars(
                select(Store).where(
                    Store.workspace_id == self._workspace_id, Store.id == store_id
                )
            )
        ).one_or_none()
        if store is None:
            return _Rejection(NOT_FOUND, None, "店铺不存在")
        location, location_rejection = await self._resolve_location(store_id, location_ref)
        if location_rejection is not None:
            return location_rejection
        assert location is not None
        level = await ProductRepository(self._session).get_inventory_level(
            self._workspace_id, location.id, listing.product_id
        )
        if level is None:
            return _Rejection(
                INVENTORY_NOT_FOUND,
                None,
                f"位置 {location.name!r} 内不存在 SKU {sku!r} 的库存记录",
            )
        decision = await self._policy.evaluate(
            workspace_id=self._workspace_id,
            store_id=store_id,
            location_id=location.id,
            product_id=listing.product_id,
            delta=delta,
            reason=reason_value,
            expected_version=level.version,
        )
        if not decision.allowed:
            policy_reason = decision.reason_code or VALIDATION_FAILED
            return _Rejection(
                _POLICY_REASON_TO_ERROR_CODE.get(policy_reason, VALIDATION_FAILED),
                policy_reason,
                decision.safe_message or policy_reason,
            )
        return PreparedAdjustInventory(
            workspace_id=self._workspace_id,
            store_ref=store_ref,
            store_id=store_id,
            location_ref=location.name,
            location_id=location.id,
            product_id=listing.product_id,
            external_store_ref=store.external_store_ref,
            external_location_ref=location.external_location_ref,
            external_product_ref=listing.external_product_ref,
            sku=sku,
            delta=delta,
            reason=reason_value,
            quantity_before=level.available_quantity,
            expected_version=level.version,
            policy_version=self._policy.policy_version,
        )

    async def _resolve_store(self, store_ref: str) -> tuple[uuid.UUID | None, _Rejection | None]:
        """解析模型可见店铺引用,保留真实拒绝原因(无匹配/歧义/越权)。"""
        try:
            store_id = await self._stores.resolve_authorized(self._actor, store_ref)
        except ProductOperationError as error:
            return None, _Rejection(error.code, None, error.detail or error.code)
        except ToolAccessError as error:
            return None, _Rejection(error.reason_code, None, str(error))
        return store_id, None

    async def _resolve_location(
        self, store_id: uuid.UUID, location_ref: str | None
    ) -> tuple[FulfillmentLocation | None, _Rejection | None]:
        """解析位置引用;歧义与缺失都保留真实规范码,不折叠成"库存不存在"。"""
        try:
            location = await resolve_location_for_store(
                self._session, self._workspace_id, store_id, location_ref
            )
        except ProductOperationError as error:
            return None, _Rejection(error.code, None, error.detail or error.code)
        return location, None

    def _to_tool_receipt(
        self,
        tool_call_id: uuid.UUID,
        prepared: PreparedAdjustInventory,
        receipt: InventoryWriteReceipt,
    ) -> ToolExecutionReceipt:
        """写回执 → Runtime ``ToolExecutionReceipt``(payload 对齐 Output Schema)。"""
        payload: dict[str, Any] | None = None
        if receipt.status is WriteStatus.APPLIED:
            result = AdjustInventoryQuantityResult(
                operation_id=receipt.operation_id,
                policy_version=prepared.policy_version,
                store_ref=prepared.store_ref,
                sku=prepared.sku,
                location_ref=prepared.location_ref,
                delta=prepared.delta,
                available_quantity=receipt.after_quantity,
                inventory_version=receipt.resource_version,
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
    """无法构造可信目标时的最小审计上下文,不臆测库存侧字段。"""
    context: dict[str, str] = {
        "operation_type": OPERATION_TYPE,
        "store_ref": str(arguments.get("store_ref", "")),
        "location_ref": str(arguments.get("location_ref", "")),
        "sku": str(arguments.get("sku", "")),
        "delta": str(arguments.get("delta", "")),
        "policy_version": policy_version,
        "rejection_detail": rejection.detail,
    }
    if rejection.rule_code is not None:
        context["rule_code"] = rejection.rule_code
    return context


__all__ = [
    "ADJUST_INVENTORY_QUANTITY_VERSION",
    "AMBIGUOUS_LOCATION",
    "INVALID_ARGUMENTS",
    "INVENTORY_NOT_FOUND",
    "OPERATION_TYPE",
    "PRODUCT_INVENTORY_WRITE_PERMISSION",
    "STABLE_KEY_ADJUST_INVENTORY_QUANTITY",
    "AdjustInventoryQuantityHandler",
    "InventoryService",
    "PreparedAdjustInventory",
    "resolve_location_for_store",
]
