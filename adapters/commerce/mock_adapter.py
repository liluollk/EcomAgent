"""确定性 Mock 平台 Adapter(计划 03 Task 5;架构文档 §3、§7)。

权威状态:``product_operations`` 的 ``listings`` 表即 Mock 平台的平台侧
权威状态(架构文档 §3:"Mock Adapter 将这些表视为外部平台的权威状态")。
价格经 :class:`ProductRepository` 的 ``version`` CAS 真正写入 ``listings``,
因此写入结果真实反映持久状态,供上层 ``authoritative_price`` 断言;Adapter
自身只额外维护 ``operation_id`` 幂等去重表(内存),记录每个操作的上次回执
与逻辑写事实,供重复提交回放与 ``count_logical_writes`` 断言。

``execute`` 执行顺序(确定性,无随机):

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
)
from agents.product_operations.persistence.models import Listing
from agents.product_operations.persistence.repository import ProductRepository
from runtime.execution.reconciliation import ReconcileResult


@dataclass(frozen=True)
class _OperationRecord:
    """operation_id 幂等记录:上次回执与逻辑写事实。"""

    receipt: ExecutionReceipt
    workspace_id: uuid.UUID
    listing_id: uuid.UUID
    logical_writes: int
    committed: bool
    applied_price: Decimal | None


class MockCommerceAdapter:
    """确定性 Mock 平台;结构化实现 :class:`CommerceAdapter` 协议。

    幂等去重表为内存状态:仅在请求到达平台写路径(第 4 步之后)时创建
    记录,写前超时与持续失败不产生记录——两者都可确定地重试。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repository = ProductRepository(session)
        self.faults = FaultInjector()
        self._operations: dict[str, _OperationRecord] = {}

    async def execute(self, command: UpdatePriceCommand) -> ExecutionReceipt:
        """按确定性顺序执行写命令;独立模拟平台事务只覆盖数据库操作。"""
        if self._session.in_transaction():
            return await self._execute(command)
        async with self._session.begin():
            return await self._execute(command)

    async def _execute(self, command: UpdatePriceCommand) -> ExecutionReceipt:
        """执行数据库 Mock 平台逻辑(不发起网络请求)。"""
        # 1. 写前超时:请求未达平台逻辑(含去重层),无任何状态变更。
        if self.faults.should_timeout_before_write():
            return self._receipt(
                command,
                ExecutionStatus.UNKNOWN,
                reason_code=REASON_TIMEOUT_BEFORE_WRITE,
            )

        # 2. 幂等去重:已见 operation_id 直接回放上次回执,不再写。
        record = self._operations.get(command.operation_id)
        if record is not None:
            return record.receipt

        # 3. 平台逻辑不可用:确定失败(非 UNKNOWN);未达写路径,不建记录。
        if self.faults.should_fail_permanently():
            return self._receipt(
                command,
                ExecutionStatus.FAILED,
                reason_code=REASON_PLATFORM_FAILURE,
            )

        # 4. 平台侧命令校验:Listing 必须存在,币种必须一致。
        listing = await self._repository.get_listing(
            command.workspace_id, command.listing_id
        )
        if listing is None:
            return self._record(
                command,
                self._receipt(
                    command,
                    ExecutionStatus.FAILED,
                    reason_code=REASON_LISTING_NOT_FOUND,
                ),
                logical_writes=0,
                committed=False,
                applied_price=None,
            )
        if listing.currency != command.currency:
            return self._record(
                command,
                self._receipt(
                    command,
                    ExecutionStatus.FAILED,
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
                self._receipt(
                    command,
                    ExecutionStatus.FAILED,
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
        receipt = ExecutionReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=ExecutionStatus.APPLIED,
            before_price=before_price,
            after_price=observed.current_price,
            listing_version=observed.version,
            external_ref=observed.external_product_ref,
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
            return ExecutionReceipt(
                operation_id=command.operation_id,
                idempotency_key=command.idempotency_key,
                status=ExecutionStatus.UNKNOWN,
                reason_code=REASON_RESPONSE_LOST_AFTER_COMMIT,
            )
        return receipt

    async def reconcile(
        self, command: ReconciliationCommand
    ) -> ReconcileResult:
        """以权威状态回答;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。

        幂等记录不存在或未提交 → 确定未应用;已提交则重读 Listing,权威
        价格与期望价格及本操作应用价一致才返回 APPLIED。
        """
        if self._session.in_transaction():
            return await self._reconcile(command)
        async with self._session.begin():
            return await self._reconcile(command)

    async def _reconcile(self, command: ReconciliationCommand) -> ReconcileResult:
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

    async def count_logical_writes(self, operation_id: str) -> int:
        """该 operation_id 已发生的逻辑写次数;未知操作返回 0。"""
        record = self._operations.get(operation_id)
        return 0 if record is None else record.logical_writes

    def _receipt(
        self,
        command: UpdatePriceCommand,
        status: ExecutionStatus,
        *,
        reason_code: str,
    ) -> ExecutionReceipt:
        return ExecutionReceipt(
            operation_id=command.operation_id,
            idempotency_key=command.idempotency_key,
            status=status,
            reason_code=reason_code,
        )

    def _record(
        self,
        command: UpdatePriceCommand,
        receipt: ExecutionReceipt,
        *,
        logical_writes: int,
        committed: bool,
        applied_price: Decimal | None,
    ) -> ExecutionReceipt:
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


if TYPE_CHECKING:
    from adapters.commerce.protocol import CommerceAdapter

    _protocol_conformance: type[CommerceAdapter] = MockCommerceAdapter


__all__ = ["MockCommerceAdapter"]
