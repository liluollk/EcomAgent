"""确定性 Mock 平台 Adapter(计划 03 Task 5;P1 Task 6 迁移到 PricingGateway)。

权威状态:``product_operations`` 的 ``listings`` 表即 Mock 平台的平台侧
权威状态(架构文档 §3:"Mock Adapter 将这些表视为外部平台的权威状态")。
价格经 :class:`ProductRepository` 的 ``version`` CAS 真正写入 ``listings``,
因此写入结果真实反映持久状态,供上层 ``authoritative_price`` 断言;Adapter
自身只额外维护 ``operation_id`` 幂等去重表(内存),记录每个操作的上次回执
与逻辑写事实,供重复提交回放与 ``count_logical_writes`` 断言。

P1 起本 Adapter 实现 :class:`PricingGateway`(``get_price`` / ``set_price`` /
``reconcile_price``);旧 ``execute`` / ``reconcile`` 保留为兼容包装,委托新方法
并映射回旧回执类型,供历史 ToolVersion 的 Run 恢复。

``set_price`` 执行顺序(确定性,无随机):

1. ``timeout_before_write`` → UNKNOWN(请求未达平台逻辑,零状态变更);
2. 幂等去重:已见 ``operation_id`` 直接回放上次回执,不再写;
3. ``permanent_failure`` → FAILED(确定失败,请求未达写路径,不建记录);
4. 平台侧命令校验(Listing 存在、币种一致),拒绝 → FAILED 且零逻辑写;
5. ``expected_version`` CAS 写入;零行命中 → FAILED/VERSION_CONFLICT,
   不覆盖新状态;
6. 写后重读 Listing,回执携带观测到的前后价格与最终版本;
7. ``response_lost_after_commit`` → UNKNOWN(写入已生效并已记录,回查
   可确认 APPLIED)。

事务边界:若调用方已有事务,仅 flush 交给调用方;若无活动事务,Mock Adapter
为模拟平台数据库操作开启并提交独立短事务。Adapter 不包含网络请求。
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from adapters.commerce.faults import FaultInjector
from adapters.commerce.protocol import (
    REASON_CURRENCY_MISMATCH,
    REASON_LISTING_NOT_FOUND,
    REASON_PLATFORM_FAILURE,
    REASON_RESPONSE_LOST_AFTER_COMMIT,
    REASON_TIMEOUT_BEFORE_WRITE,
    REASON_VERSION_CONFLICT,
    ExecutionReceipt,
    ExecutionStatus,
    ReconciliationCommand,
    UpdatePriceCommand,
    is_retryable_reason_code,
)
from agents.product_operations.persistence.models import Listing
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.pricing.gateway import (
    PriceQuery,
    PriceSnapshot,
    PriceUnavailable,
    PriceWriteReceipt,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.shared.write_contracts import WriteStatus
from runtime.execution.reconciliation import ReconcileResult


@dataclass(frozen=True)
class _OperationRecord:
    """operation_id 幂等记录:上次回执与逻辑写事实。"""

    receipt: PriceWriteReceipt
    workspace_id: uuid.UUID
    listing_id: uuid.UUID
    logical_writes: int
    committed: bool
    applied_price: Decimal | None


class MockCommerceAdapter:
    """确定性 Mock 平台;结构化实现 :class:`PricingGateway` 协议。

    幂等去重表为内存状态:仅在请求到达平台写路径(第 4 步之后)时创建记录;
    写前超时(UNKNOWN)与持续失败(FAILED)都不产生记录。但"不产生记录"不等于
    "值得重试":两者都带固定 ``reason_code``,重排队只会得到同一结论——按
    Runtime 的确定失败语义,它们的 ToolCall 都落终态,而非自动重试。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = ProductRepository(session)
        self.faults = FaultInjector()
        self._operations: dict[str, _OperationRecord] = {}

    # ---- PricingGateway ----

    async def get_price(self, query: PriceQuery) -> PriceSnapshot:
        """读取平台侧权威价格;Listing 缺失或币种不符即不可读。"""
        if self._session.in_transaction():
            return await self._get_price(query)
        async with self._session.begin():
            return await self._get_price(query)

    async def _get_price(self, query: PriceQuery) -> PriceSnapshot:
        listing = await self._repository.get_listing(
            query.workspace_id, query.listing_id
        )
        if listing is None or listing.currency != query.currency:
            raise PriceUnavailable("listing is not readable on the platform")
        return PriceSnapshot(
            price=listing.current_price,
            currency=listing.currency,
            listing_version=listing.version,
            external_ref=listing.external_product_ref,
        )

    async def set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        """按确定性顺序执行写命令;独立模拟平台事务只覆盖数据库操作。"""
        if self._session.in_transaction():
            return await self._set_price(command)
        async with self._session.begin():
            return await self._set_price(command)

    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        """以权威状态回答;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。

        幂等记录不存在或未提交 → 确定未应用;已提交则重读 Listing,权威
        价格与期望价格及本操作应用价一致才返回 APPLIED。
        """
        if self._session.in_transaction():
            return await self._reconcile_price(command)
        async with self._session.begin():
            return await self._reconcile_price(command)

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

    async def count_logical_writes(self, operation_id: str) -> int:
        """该 operation_id 已发生的逻辑写次数;未知操作返回 0。"""
        record = self._operations.get(operation_id)
        return 0 if record is None else record.logical_writes

    # ---- 内部 ----

    async def _set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        """执行数据库 Mock 平台逻辑(不发起网络请求)。"""
        # 1. 写前超时:请求未达平台逻辑(含去重层),无任何状态变更。
        if self.faults.should_timeout_before_write():
            return _receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_TIMEOUT_BEFORE_WRITE,
            )

        # 2. 幂等去重:已见 operation_id 直接回放上次回执,不再写。
        record = self._operations.get(command.operation_id)
        if record is not None:
            return record.receipt

        # 3. 平台逻辑不可用:确定失败(非 UNKNOWN);未达写路径,不建记录。
        if self.faults.should_fail_permanently():
            return _receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.FAILED,
                reason_code=REASON_PLATFORM_FAILURE,
            )

        # 4. 平台侧命令校验:Listing 必须存在,币种必须一致。
        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if listing is None:
            return self._record(
                command,
                _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_LISTING_NOT_FOUND,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )
        if listing.currency != command.currency:
            return self._record(
                command,
                _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_CURRENCY_MISMATCH,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )

        before_price = listing.current_price

        # 5. expected_version CAS 写入;零行命中即版本过期,不覆盖新状态。
        updated = await self._repository.update_price(
            workspace_id=command.workspace_id,
            listing_id=command.listing_id,
            target_price=command.target_price,
            expected_version=command.expected_version,
        )
        if updated is None:
            return self._record(
                command,
                _receipt(
                    command.operation_id,
                    command.idempotency_key,
                    WriteStatus.FAILED,
                    reason_code=REASON_VERSION_CONFLICT,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )

        # 6. 写后重读,回执携带观测到的价格与版本。
        reread = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        observed: Listing = reread if reread is not None else updated
        receipt = PriceWriteReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=WriteStatus.APPLIED,
            before_price=before_price,
            after_price=observed.current_price,
            resource_version=observed.version,
            external_ref=observed.external_product_ref,
            changed=True,
        )
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            listing_id=command.listing_id,
            logical_writes=1,
            committed=True,
            applied_price=command.target_price,
        )

        # 7. 响应丢失:写入已生效,调用方只能得到 UNKNOWN,回查恢复。
        if self.faults.should_lose_response_after_commit():
            return _receipt(
                command.operation_id,
                command.idempotency_key,
                WriteStatus.UNKNOWN,
                reason_code=REASON_RESPONSE_LOST_AFTER_COMMIT,
            )
        return receipt

    async def _reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        if self.faults.consume_reconcile_unknown():
            return ReconcileResult.UNKNOWN

        record = self._operations.get(command.operation_id)
        if record is None or not record.committed:
            return ReconcileResult.NOT_APPLIED

        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if (
            listing is not None
            and record.applied_price is not None
            and listing.current_price == command.expected_price
            and command.expected_price == record.applied_price
        ):
            return ReconcileResult.APPLIED
        return ReconcileResult.NOT_APPLIED

    def _record(
        self,
        command: SetListingPriceCommand,
        receipt: PriceWriteReceipt,
        *,
        logical_writes: int,
        committed: bool,
        applied_price: Decimal | None,
    ) -> PriceWriteReceipt:
        """登记幂等记录并返回回执(回执与记录所见一致,供回放)。"""
        self._operations[command.operation_id] = _OperationRecord(
            receipt=receipt,
            workspace_id=command.workspace_id,
            listing_id=command.listing_id,
            logical_writes=logical_writes,
            committed=committed,
            applied_price=applied_price,
        )
        return receipt


def _receipt(
    operation_id: str,
    idempotency_key: str,
    status: WriteStatus,
    *,
    reason_code: str,
) -> PriceWriteReceipt:
    """构造无观测的价格回执(UNKNOWN/FAILED 只带 reason_code)。"""
    return PriceWriteReceipt(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        status=status,
        reason_code=reason_code,
    )


def _to_set_price_command(command: UpdatePriceCommand) -> SetListingPriceCommand:
    """旧写命令 → PricingGateway 写命令(字段一一对应,``sku`` 不参与平台判定)。"""
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
    from adapters.commerce.protocol import CommerceAdapter
    from agents.product_operations.pricing.gateway import PricingGateway

    _protocol_conformance: type[CommerceAdapter] = MockCommerceAdapter
    _pricing_conformance: type[PricingGateway] = MockCommerceAdapter


__all__ = ["MockCommerceAdapter"]
