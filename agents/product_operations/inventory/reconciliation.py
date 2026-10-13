"""Inventory 回查端点(P1 Task 7;设计 §5.3、§6)。

:class:`InventoryReconciler` 只使用 ``Operation.expected_state`` 构造平台回查命令:
位置与商品标识、``quantity_before`` 与 ``expected_quantity_after`` 全部来自写前
持久化的业务预期,**绝不**从最新对话、当前 Agent Prompt 或模型文本恢复(设计 §6)。
持久化的 ``workspace_id`` 必须与当前 Workspace 一致,否则 ``VALIDATION_FAILED``。

``operation_id`` 是 Runtime 的执行边界分配、写进 Operation 行的权威身份,不在
``expected_state`` 中;因此应用层按 Operation 逐条构造本 Reconciler,由其携带该身份。
``reconcile`` 的签名与
:class:`~agents.product_operations.shared.reconciliation.ProductOperationReconciler`
协议一致。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from agents.product_operations.inventory.gateway import (
    InventoryGateway,
    ReconcileInventoryCommand,
)
from agents.product_operations.inventory.service import OPERATION_TYPE
from agents.product_operations.shared.errors import (
    VALIDATION_FAILED,
    ProductOperationError,
)
from agents.product_operations.shared.reconciliation import (
    resolve_persisted_workspace_id,
)
from runtime.execution.reconciliation import ReconcileResult


_REQUIRED_KEYS: Final = (
    "operation_type",
    "workspace_id",
    "store_id",
    "location_id",
    "product_id",
    "quantity_before",
    "expected_quantity_after",
    "external_store_ref",
    "external_location_ref",
    "external_product_ref",
)

_INT_KEYS: Final = ("quantity_before", "expected_quantity_after")


def build_reconcile_inventory_command(
    *,
    operation_id: str,
    expected_state: Mapping[str, Any],
    workspace_id: uuid.UUID,
) -> ReconcileInventoryCommand:
    """从 ``expected_state`` 构造回查命令;缺字段或类型不符即 fail closed。

    不做任何"默认回查"猜测:缺少可信业务目标时抛 ``VALIDATION_FAILED``,由应用层
    记录,而不是拿残缺上下文去查平台。持久化的 ``workspace_id`` 必须与
    ``workspace_id`` 参数一致,否则同样拒绝(不让当前 Workspace 覆盖持久预期)。
    """
    missing = [key for key in _REQUIRED_KEYS if key not in expected_state]
    if missing:
        raise ProductOperationError(
            VALIDATION_FAILED,
            f"expected_state 缺少回查必需字段: {', '.join(missing)}",
        )
    if expected_state["operation_type"] != OPERATION_TYPE:
        raise ProductOperationError(
            VALIDATION_FAILED,
            f"operation_type 不是 {OPERATION_TYPE}: {expected_state['operation_type']!r}",
        )
    persisted_workspace_id = resolve_persisted_workspace_id(
        expected_state, current_workspace_id=workspace_id
    )
    try:
        store_id = uuid.UUID(str(expected_state["store_id"]))
        location_id = uuid.UUID(str(expected_state["location_id"]))
        product_id = uuid.UUID(str(expected_state["product_id"]))
        quantities = {key: int(expected_state[key]) for key in _INT_KEYS}
    except (ValueError, TypeError) as error:
        raise ProductOperationError(
            VALIDATION_FAILED, "expected_state 中的标识或数量无法解析"
        ) from error
    return ReconcileInventoryCommand(
        operation_id=operation_id,
        workspace_id=persisted_workspace_id,
        store_id=store_id,
        location_id=location_id,
        product_id=product_id,
        external_store_ref=str(expected_state["external_store_ref"]),
        external_location_ref=str(expected_state["external_location_ref"]),
        external_product_ref=str(expected_state["external_product_ref"]),
        quantity_before=quantities["quantity_before"],
        expected_quantity_after=quantities["expected_quantity_after"],
    )


class InventoryReconciler:
    """``ADJUST_INVENTORY`` 的回查端点;只依赖注入的 :class:`InventoryGateway`。"""

    def __init__(
        self,
        gateway: InventoryGateway,
        *,
        workspace_id: uuid.UUID,
        operation_id: str,
    ) -> None:
        self._gateway = gateway
        self._workspace_id = workspace_id
        self._operation_id = operation_id

    async def reconcile(self, expected_state: Mapping[str, Any]) -> ReconcileResult:
        """按 ``expected_state`` 回查平台;只返回 APPLIED/NOT_APPLIED/UNKNOWN。"""
        command = build_reconcile_inventory_command(
            operation_id=self._operation_id,
            expected_state=expected_state,
            workspace_id=self._workspace_id,
        )
        return await self._gateway.reconcile_inventory(command)


__all__ = ["InventoryReconciler", "build_reconcile_inventory_command"]
