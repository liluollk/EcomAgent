"""ProductOperations 的模块化 Operation 回查应用服务。"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.inventory.gateway import InventoryGateway
from agents.product_operations.inventory.reconciliation import InventoryReconciler
from agents.product_operations.inventory.service import OPERATION_TYPE as INVENTORY_OPERATION
from agents.product_operations.listing.gateway import ListingGateway
from agents.product_operations.listing.reconciliation import ListingReconciler
from agents.product_operations.listing.service import OPERATION_TYPE as LISTING_OPERATION
from agents.product_operations.pricing.gateway import PricingGateway
from agents.product_operations.pricing.reconciliation import PriceReconciler
from agents.product_operations.pricing.service import OPERATION_TYPE as PRICING_OPERATION
from agents.product_operations.shared.reconciliation import OperationReconcilerRegistry
from runtime.execution.reconciliation import ReconciliationService
from runtime.orchestration.limits import RunLimits
from runtime.persistence.base import OperationStatus, RunNextStep, RunStatus
from runtime.persistence.models.execution import Operation, ToolCall
from runtime.persistence.repositories.runs import RunRepository


def build_reconciler_registry(
    *,
    pricing_gateway: PricingGateway,
    inventory_gateway: InventoryGateway,
    listing_gateway: ListingGateway,
    workspace_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> OperationReconcilerRegistry:
    """为单个持久化 Operation 构造三种受控 ``operation_type`` 的分发表。"""
    registry = OperationReconcilerRegistry()
    registry.register(
        PRICING_OPERATION,
        PriceReconciler(
            pricing_gateway,
            workspace_id=workspace_id,
            operation_id=str(operation_id),
        ),
    )
    registry.register(
        INVENTORY_OPERATION,
        InventoryReconciler(
            inventory_gateway,
            workspace_id=workspace_id,
            operation_id=str(operation_id),
        ),
    )
    registry.register(
        LISTING_OPERATION,
        ListingReconciler(
            listing_gateway,
            workspace_id=workspace_id,
            operation_id=str(operation_id),
        ),
    )
    return registry


class ReconciliationSourceError(RuntimeError):
    """等待回查的 Run 缺少唯一的不确定 Operation。

    这是数据不一致,重试同一份数据不会变好,因此标记为不可重试:执行器据此
    直接把 Run 收敛到 FAILED,而不是让它停在等待态反复空转。
    """

    retryable = False
    code = "RECONCILE_SOURCE_INVALID"


class ProductOperationsRecovery:
    """领取 UNKNOWN Operation，并按持久化 ``operation_type`` 回查后提交结论。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        pricing_gateway: PricingGateway,
        inventory_gateway: InventoryGateway,
        listing_gateway: ListingGateway,
        run_limits: RunLimits,
        before_external_call: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._pricing_gateway = pricing_gateway
        self._inventory_gateway = inventory_gateway
        self._listing_gateway = listing_gateway
        self._run_limits = run_limits
        self._before_external_call = before_external_call

    async def reconcile_once(self, run_id: uuid.UUID) -> bool:
        """完成一次回查;返回是否需要继续推进该 Run。"""
        loaded = await self._load_unknown_operation(run_id)
        if loaded is None:
            return False
        operation_id, expected_state, expected_state_version = loaded
        operation_type = expected_state.get("operation_type")
        if not isinstance(operation_type, str):
            raise TypeError("UNKNOWN operation expected_state has no operation_type")
        registry = build_reconciler_registry(
            pricing_gateway=self._pricing_gateway,
            inventory_gateway=self._inventory_gateway,
            listing_gateway=self._listing_gateway,
            workspace_id=self._workspace_id,
            operation_id=operation_id,
        )
        if self._before_external_call is not None:
            self._before_external_call()
        verdict = await registry.require(operation_type).reconcile(expected_state)

        session = self._session_factory()
        try:
            async with session.begin():
                outcome = await ReconciliationService(
                    session,
                    workspace_id=self._workspace_id,
                    actor_id=self._actor_id,
                    run_id=run_id,
                    operation_id=operation_id,
                    expected_state_version=expected_state_version,
                    reconcile_budget=self._run_limits.reconcile_budget,
                    write_retry_budget=self._run_limits.write_retry_budget,
                ).apply_result(verdict)
                return outcome.next_step is RunNextStep.ADVANCE
        finally:
            await session.close()

    async def _load_unknown_operation(
        self, run_id: uuid.UUID
    ) -> tuple[uuid.UUID, Mapping[str, Any], int] | None:
        """读取该 Run 唯一的不确定 Operation 与 Run 的当前 state_version。

        ``EXECUTING`` 与 ``UNKNOWN`` 都算不确定:进程可能在外部调用期间退出,
        此时 Operation 停在 EXECUTING,但外部写入是否生效同样未知。
        """
        session = self._session_factory()
        try:
            run = await RunRepository(session).get(self._workspace_id, run_id)
            if run is None or run.status is not RunStatus.WAITING_RECONCILIATION:
                return None
            operations = (
                await session.scalars(
                    select(Operation)
                    .join(ToolCall, ToolCall.id == Operation.tool_call_id)
                    .where(
                        Operation.workspace_id == self._workspace_id,
                        ToolCall.workspace_id == self._workspace_id,
                        ToolCall.run_id == run_id,
                        Operation.status.in_(
                            (OperationStatus.EXECUTING, OperationStatus.UNKNOWN)
                        ),
                    )
                )
            ).all()
            if len(operations) != 1:
                # 数据不一致:等待回查却没有唯一的不确定 Operation。静默返回
                # None 会让这个 Run 永久停在等待态,没有任何路径再处理它。
                raise ReconciliationSourceError(
                    f"run {run_id} waits for reconciliation but has "
                    f"{len(operations)} uncertain operations"
                )
            operation = operations[0]
            if operation.expected_state is None:
                raise ReconciliationSourceError(
                    f"UNKNOWN operation {operation.operation_id} has no expected_state"
                )
            return (
                operation.operation_id,
                dict(operation.expected_state),
                run.state_version,
            )
        finally:
            await session.close()


__all__ = [
    "ProductOperationsRecovery",
    "ReconciliationSourceError",
    "build_reconciler_registry",
]
