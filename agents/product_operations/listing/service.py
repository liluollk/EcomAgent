"""Listing 查询与发布状态写入(P1 Task 5 查询;Task 8 增加写入)。

- :class:`ListingService` 提供确定性的刊登读取:经授权的
  :class:`StoreReferenceResolver` 解析 ``store_ref``,再以
  ``workspace_id + store_id + sku`` 查询。未发布与已发布的 Listing 都可读取:
  发布状态是读取结果,不是过滤条件(设计 §4.5:"库存为零不等价于取消发布")。
- :class:`ChangePublicationHandler` 实现 Runtime 两阶段写协议:``prepare`` 在无
  外部副作用的前提下解析可信资源、读取 Listing 版本、执行
  :class:`~agents.product_operations.listing.policy.ListingPolicy` 执行校验并构造
  可回放的 ``expected_state``(``listing_id`` / 目标发布状态 / ``expected_version``
  / 原因);``execute_prepared`` 只把该命令交给注入的 ``ListingGateway``,不接受
  模型参数,异常交由 ToolExecutor 归 UNKNOWN。

发布方向由 Handler 子类固定(:class:`PublishListingHandler` → ``PUBLISHED``、
:class:`UnpublishListingHandler` → ``UNPUBLISHED``),不是模型参数:同一个
``operation_type`` 下的方向来自**已固定的 ToolVersion**,恢复时据此判定目标状态。

``prepare`` 失败返回**类型化拒绝**:保留真实规范错误码(NOT_FOUND /
AMBIGUOUS_STORE_REFERENCE / RESOURCE_SCOPE_DENIED / LISTING_NOT_FOUND /
CAPABILITY_UNSUPPORTED / INVALID_STATE_TRANSITION / VALIDATION_FAILED +
rule_code),不把越权与规则拒绝统一伪装成"资源不存在"。

导入方向:本模块不 import adapters(平台桥接由组合根完成)。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus, ProductStatus
from agents.product_operations.input.normalizer import normalize_arguments
from agents.product_operations.input.profiles import p1_fields_for
from agents.product_operations.input.references import StoreReferenceResolver
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
    ListingGateway,
)
from agents.product_operations.listing.models import (
    ListingPublicationResult,
    ListingSnapshot,
)
from agents.product_operations.listing.policy import (
    INVALID_STATE_TRANSITION,
    ListingPolicy,
    ListingPolicyConfig,
)
from agents.product_operations.listing.policy import (
    LISTING_NOT_FOUND as POLICY_LISTING_NOT_FOUND,
)
from agents.product_operations.persistence.models import Listing, Product, Store
from agents.product_operations.shared.errors import (
    CAPABILITY_UNSUPPORTED,
    NOT_FOUND,
    VALIDATION_FAILED,
    ProductOperationError,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from agents.product_operations.tools.access import authorize_product_read
from agents.product_operations.tools.errors import ToolAccessError
from runtime.access.actor import ActorContext
from runtime.execution.tool_executor import PreparedToolWrite, PreparedWriteRejection
from runtime.execution.tool_result_processor import ToolExecutionReceipt


STABLE_KEY_PUBLISH_LISTING: Final = "publish_listing"
PUBLISH_LISTING_VERSION: Final = 1
STABLE_KEY_UNPUBLISH_LISTING: Final = "unpublish_listing"
UNPUBLISH_LISTING_VERSION: Final = 1

PRODUCT_LISTING_WRITE_PERMISSION: Final = "product.listing.write"
"""刊登写权限;与 ``product.price.write`` / ``product.inventory.write`` 对称。"""

OPERATION_TYPE: Final = "CHANGE_LISTING_PUBLICATION"
"""本能力的受控 ``operation_type``;模型不可见、不可填写。

发布与取消发布共用它:方向由 ``expected_state`` 中的目标状态表达,而该状态来自
prepare 阶段已固定的 ToolVersion,不是模型参数。恢复时据此分发到
:class:`~agents.product_operations.listing.reconciliation.ListingReconciler`。
"""

LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
"""Listing 在 (workspace, store, sku) 下确实不存在时的规范错误码。"""

INVALID_ARGUMENTS: Final = "INVALID_ARGUMENTS"
"""参数形状非法时的规则码(作 ``VALIDATION_FAILED`` 的 ``rule_code``)。"""

_POLICY_REASON_TO_ERROR_CODE: Final[dict[str, str]] = {
    # 策略层的具体原因码 → 规范的 error_code;未列出的规则违规一律归
    # ``VALIDATION_FAILED``,具体规则保留在 ``rule_code``,不伪造成"资源不存在"。
    POLICY_LISTING_NOT_FOUND: LISTING_NOT_FOUND,
    CAPABILITY_UNSUPPORTED: CAPABILITY_UNSUPPORTED,
    INVALID_STATE_TRANSITION: INVALID_STATE_TRANSITION,
}

PRODUCT_DISABLED_ISSUE = "PRODUCT_DISABLED"

PublicationState = Literal["PUBLISHED", "UNPUBLISHED", "UNKNOWN"]


def _publication_state(status: ListingStatus) -> PublicationState:
    """受控发布状态;未知取值收敛为 ``UNKNOWN`` 而不是猜测。"""
    if status is ListingStatus.PUBLISHED:
        return "PUBLISHED"
    if status is ListingStatus.UNPUBLISHED:
        return "UNPUBLISHED"
    return "UNKNOWN"


class ListingService:
    """单个 Listing 的只读应用服务。"""

    def __init__(
        self, session: AsyncSession, *, stores: StoreReferenceResolver | None = None
    ) -> None:
        self._session = session
        self._stores = stores or StoreReferenceResolver(session)

    async def get_listing(
        self, *, actor: ActorContext, store_ref: str, sku: str
    ) -> ListingSnapshot:
        """读取授权店铺内单个 SKU 的 Listing 快照。

        店铺引用无法解析 → ``NOT_FOUND`` / ``AMBIGUOUS_STORE_REFERENCE``;
        越权店铺 → ``RESOURCE_SCOPE_DENIED``;店铺内无该 SKU 的上架记录 →
        ``NOT_FOUND``。
        """
        authorize_product_read(actor)
        store_id = await self._stores.resolve_authorized(actor, store_ref)
        stmt = (
            select(Product, Listing)
            .join_from(Product, Listing, Listing.product_id == Product.id)
            .where(
                Product.workspace_id == actor.workspace_id,
                Listing.workspace_id == actor.workspace_id,
                Listing.store_id == store_id,
                Product.sku == sku,
            )
            .order_by(Listing.id)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            raise ProductOperationError(
                NOT_FOUND, f"店铺 {store_ref!r} 中不存在 SKU {sku!r} 的上架记录"
            )
        product, listing = row
        issues: list[str] = []
        if product.status is ProductStatus.DISABLED:
            issues.append(PRODUCT_DISABLED_ISSUE)
        return ListingSnapshot(
            store_ref=store_ref,
            sku=product.sku,
            title=product.title,
            current_price=listing.current_price,
            currency=listing.currency,
            publication_state=_publication_state(listing.status),
            listing_version=listing.version,
            issues=tuple(issues),
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
class PreparedChangePublication:
    """prepare 阶段解析出的可信资源与目标发布状态(不含 ``operation_id``)。"""

    workspace_id: uuid.UUID
    store_ref: str
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    sku: str
    target_state: ListingStatus
    reason: str | None
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
            "target_state": self.target_state.value,
            "reason": self.reason,
            "expected_version": self.expected_version,
            "policy_version": self.policy_version,
            "external_store_ref": self.external_store_ref,
            "external_product_ref": self.external_product_ref,
        }

    def to_command(
        self, operation_id: uuid.UUID, idempotency_key: str
    ) -> ChangePublicationCommand:
        """注入执行边界分配的 ``operation_id`` 与幂等键,得到平台写命令。"""
        return ChangePublicationCommand(
            operation_id=str(operation_id),
            idempotency_key=idempotency_key,
            workspace_id=self.workspace_id,
            store_id=self.store_id,
            listing_id=self.listing_id,
            external_store_ref=self.external_store_ref,
            external_product_ref=self.external_product_ref,
            sku=self.sku,
            reason=self.reason,
            expected_version=self.expected_version,
        )


@dataclass(frozen=True)
class _Rejection:
    """prepare 阶段无法构造可信写命令时的类型化拒绝。

    ``error_code`` 是规范错误码(NOT_FOUND / AMBIGUOUS_STORE_REFERENCE /
    RESOURCE_SCOPE_DENIED / LISTING_NOT_FOUND / CAPABILITY_UNSUPPORTED /
    INVALID_STATE_TRANSITION / VALIDATION_FAILED),``rule_code`` 是更具体的规则
    (如原因非法、参数形状非法),``detail`` 是安全诊断文案。三者都进审计上下文:
    把越权、能力缺失与规则拒绝统一伪装成"资源不存在"会破坏安全归因与模型纠错。
    """

    error_code: str
    rule_code: str | None
    detail: str


class ChangePublicationHandler:
    """刊登发布状态写入的外部执行端点(Runtime ``WriteToolHandler``)。

    绑定请求级 Actor 与 Session:``store_ref`` 是模型可见引用,必须经授权的
    :class:`StoreReferenceResolver` 解析;权限、资源范围与 ExecutionMode 已由
    PreToolUse 完成,``prepare`` 只做服务端补全与最终业务校验(设计 §5.2)。

    子类固定 ``STABLE_KEY``(字段集合与 Tool 一致)与 ``TARGET_STATE``(发布方向)。
    """

    STABLE_KEY: ClassVar[str]
    TARGET_STATE: ClassVar[ListingStatus]

    def __init__(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        gateway: ListingGateway,
        policy_config: ListingPolicyConfig,
        stores: StoreReferenceResolver | None = None,
    ) -> None:
        self._actor = actor
        self._workspace_id = actor.workspace_id
        self._session = session
        self._gateway = gateway
        self._policy = ListingPolicy(session, policy_config)
        self._stores = stores or StoreReferenceResolver(session)

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> PreparedToolWrite:
        """解析引用 → 读取版本 → 执行校验 → 可信命令 + ``expected_state``。

        任一环节失败都返回**类型化**拒绝:保留真实 ``error_code``(资源不存在、
        歧义引用、越权、能力缺失、非法状态迁移、规则校验失败)与其具体规则码,
        不统一伪装成"Listing 不存在"。
        """
        arguments = normalize_arguments(
            dict(normalized_arguments), fields=p1_fields_for(self.STABLE_KEY)
        )
        try:
            resolved = await self._resolve(arguments)
        except BaseException:
            await self._session.rollback()
            raise
        # Listing 读取事务到此结束:平台 Adapter 自行开启并提交它的写事务。
        await self._session.commit()
        if isinstance(resolved, _Rejection):
            context = _rejection_context(
                arguments, self.TARGET_STATE, self._policy.policy_version, resolved
            )
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
        prepared_publication = cast("PreparedChangePublication", prepared.command)
        command = prepared_publication.to_command(operation_id, idempotency_key)
        receipt = await self._call_gateway(command)
        return self._to_tool_receipt(tool_call_id, prepared_publication, receipt)

    async def _call_gateway(self, command: ChangePublicationCommand) -> WriteReceipt:
        """按固定的目标方向调用对应 Port 方法(方向不是模型参数)。"""
        if self.TARGET_STATE is ListingStatus.PUBLISHED:
            return await self._gateway.publish_listing(command)
        return await self._gateway.unpublish_listing(command)

    async def _resolve(
        self, arguments: Mapping[str, Any]
    ) -> PreparedChangePublication | _Rejection:
        """服务端补全:解析店铺/Listing,执行 ListingPolicy 校验。"""
        store_ref = arguments.get("store_ref")
        sku = arguments.get("sku")
        reason = arguments.get("reason")
        if not isinstance(store_ref, str) or not isinstance(sku, str):
            return _Rejection(
                VALIDATION_FAILED, INVALID_ARGUMENTS, "store_ref/sku 必须是字符串"
            )
        reason_value = getattr(reason, "value", reason)
        if reason_value is not None and not isinstance(reason_value, str):
            return _Rejection(
                VALIDATION_FAILED, INVALID_ARGUMENTS, "reason 必须是受控枚举或省略"
            )

        store_id, store_rejection = await self._resolve_store(store_ref)
        if store_rejection is not None:
            return store_rejection
        assert store_id is not None
        listing = await _resolve_listing(self._session, self._workspace_id, store_id, sku)
        if listing is None:
            return _Rejection(
                LISTING_NOT_FOUND, None, f"店铺内不存在 SKU {sku!r} 的 Listing"
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

        decision = await self._policy.evaluate(
            workspace_id=self._workspace_id,
            listing_id=listing.id,
            target_state=self.TARGET_STATE,
            reason=reason_value,
        )
        if not decision.allowed:
            policy_reason = decision.reason_code or VALIDATION_FAILED
            return _Rejection(
                _POLICY_REASON_TO_ERROR_CODE.get(policy_reason, VALIDATION_FAILED),
                policy_reason,
                decision.safe_message or policy_reason,
            )
        return PreparedChangePublication(
            workspace_id=self._workspace_id,
            store_ref=store_ref,
            store_id=store_id,
            listing_id=listing.id,
            external_store_ref=store.external_store_ref,
            external_product_ref=listing.external_product_ref,
            sku=sku,
            target_state=self.TARGET_STATE,
            reason=reason_value,
            expected_version=listing.version,
            policy_version=self._policy.policy_version,
        )

    async def _resolve_store(
        self, store_ref: str
    ) -> tuple[uuid.UUID | None, _Rejection | None]:
        """解析模型可见店铺引用,保留真实拒绝原因(无匹配/歧义/越权)。"""
        try:
            store_id = await self._stores.resolve_authorized(self._actor, store_ref)
        except ProductOperationError as error:
            return None, _Rejection(error.code, None, error.detail or error.code)
        except ToolAccessError as error:
            return None, _Rejection(error.reason_code, None, str(error))
        return store_id, None

    def _to_tool_receipt(
        self,
        tool_call_id: uuid.UUID,
        prepared: PreparedChangePublication,
        receipt: WriteReceipt,
    ) -> ToolExecutionReceipt:
        """写回执 → Runtime ``ToolExecutionReceipt``(payload 对齐 Output Schema)。"""
        payload: dict[str, Any] | None = None
        if receipt.status is WriteStatus.APPLIED:
            result = ListingPublicationResult(
                operation_id=receipt.operation_id,
                policy_version=prepared.policy_version,
                store_ref=prepared.store_ref,
                sku=prepared.sku,
                publication_state=self.TARGET_STATE.value,
                reason=prepared.reason,
                listing_version=receipt.resource_version,
                external_ref=receipt.external_ref,
                changed=receipt.changed,
            )
            payload = result.model_dump(mode="json", exclude_none=True)
        return ToolExecutionReceipt(
            tool_call_id=tool_call_id,
            status=receipt.status.value,
            payload=payload,
            external_reference=receipt.external_ref,
            error_code=receipt.reason_code,
        )


class PublishListingHandler(ChangePublicationHandler):
    """``publish_listing`` 写 Handler:目标状态固定为 ``PUBLISHED``。"""

    STABLE_KEY = STABLE_KEY_PUBLISH_LISTING
    TARGET_STATE = ListingStatus.PUBLISHED


class UnpublishListingHandler(ChangePublicationHandler):
    """``unpublish_listing`` 写 Handler:目标状态固定为 ``UNPUBLISHED``。"""

    STABLE_KEY = STABLE_KEY_UNPUBLISH_LISTING
    TARGET_STATE = ListingStatus.UNPUBLISHED


def _fingerprint(expected_state: Mapping[str, Any]) -> str:
    """``expected_state`` 的规范化 JSON 指纹(随 Attempt 留痕)。"""
    canonical = json.dumps(expected_state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rejection_context(
    arguments: Mapping[str, Any],
    target_state: ListingStatus,
    policy_version: str,
    rejection: _Rejection,
) -> dict[str, str]:
    """无法构造可信目标时的最小审计上下文,不臆测 Listing 侧字段。"""
    context: dict[str, str] = {
        "operation_type": OPERATION_TYPE,
        "store_ref": str(arguments.get("store_ref", "")),
        "sku": str(arguments.get("sku", "")),
        "target_state": target_state.value,
        "reason": str(arguments.get("reason", "")),
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
    "PRODUCT_DISABLED_ISSUE",
    "PRODUCT_LISTING_WRITE_PERMISSION",
    "PUBLISH_LISTING_VERSION",
    "STABLE_KEY_PUBLISH_LISTING",
    "STABLE_KEY_UNPUBLISH_LISTING",
    "UNPUBLISH_LISTING_VERSION",
    "ChangePublicationHandler",
    "ListingService",
    "PreparedChangePublication",
    "PublicationState",
    "PublishListingHandler",
    "UnpublishListingHandler",
]
