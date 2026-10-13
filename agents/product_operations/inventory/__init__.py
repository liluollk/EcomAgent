"""Inventory 能力模块(P1 Task 7;设计 §3、§4.4)。

只负责库存:确定性库存读取、增量调整与回查。拥有自己的 ``InventoryGateway``
Port、``InventoryPolicy`` 与应用服务;价格与刊登分别属于 ``pricing`` 与
``listing`` 模块,不在此处承载。
"""

from __future__ import annotations

from agents.product_operations.inventory.gateway import (
    REASON_INVENTORY_NOT_FOUND,
    AdjustInventoryCommand,
    InventoryGateway,
    InventoryLevelSnapshot,
    InventoryQuery,
    InventoryUnavailable,
    InventoryWriteReceipt,
    ReconcileInventoryCommand,
    derive_idempotency_key,
)
from agents.product_operations.inventory.models import (
    INVALID_DELTA,
    INVALID_REASON,
    AdjustInventoryQuantityInput,
    AdjustInventoryQuantityResult,
    AmbiguousLocationError,
    GetInventoryLevelInput,
    InventoryAdjustmentReason,
    InventoryLevelResult,
)
from agents.product_operations.inventory.policy import (
    InventoryPolicy,
    InventoryPolicyConfig,
    InventoryPolicyDecision,
)
from agents.product_operations.inventory.reconciliation import (
    InventoryReconciler,
    build_reconcile_inventory_command,
)
from agents.product_operations.inventory.service import (
    OPERATION_TYPE,
    PRODUCT_INVENTORY_WRITE_PERMISSION,
    STABLE_KEY_ADJUST_INVENTORY_QUANTITY,
    AdjustInventoryQuantityHandler,
    InventoryService,
    PreparedAdjustInventory,
    resolve_location_for_store,
)
from agents.product_operations.inventory.tools import (
    ADJUST_INVENTORY_QUANTITY_DEFINITION,
    ADJUST_INVENTORY_QUANTITY_VERSION,
    GET_INVENTORY_LEVEL_BINDING,
    GET_INVENTORY_LEVEL_DEFINITION,
    GET_INVENTORY_LEVEL_VERSION,
    STABLE_KEY_GET_INVENTORY_LEVEL,
    build_adjust_inventory_quantity_binding,
    get_inventory_level,
)


__all__ = [
    "ADJUST_INVENTORY_QUANTITY_DEFINITION",
    "ADJUST_INVENTORY_QUANTITY_VERSION",
    "GET_INVENTORY_LEVEL_BINDING",
    "GET_INVENTORY_LEVEL_DEFINITION",
    "GET_INVENTORY_LEVEL_VERSION",
    "INVALID_DELTA",
    "INVALID_REASON",
    "OPERATION_TYPE",
    "PRODUCT_INVENTORY_WRITE_PERMISSION",
    "REASON_INVENTORY_NOT_FOUND",
    "STABLE_KEY_ADJUST_INVENTORY_QUANTITY",
    "STABLE_KEY_GET_INVENTORY_LEVEL",
    "AdjustInventoryCommand",
    "AdjustInventoryQuantityHandler",
    "AdjustInventoryQuantityInput",
    "AdjustInventoryQuantityResult",
    "AmbiguousLocationError",
    "GetInventoryLevelInput",
    "InventoryAdjustmentReason",
    "InventoryGateway",
    "InventoryLevelResult",
    "InventoryLevelSnapshot",
    "InventoryPolicy",
    "InventoryPolicyConfig",
    "InventoryPolicyDecision",
    "InventoryQuery",
    "InventoryReconciler",
    "InventoryService",
    "InventoryUnavailable",
    "InventoryWriteReceipt",
    "PreparedAdjustInventory",
    "ReconcileInventoryCommand",
    "build_adjust_inventory_quantity_binding",
    "build_reconcile_inventory_command",
    "derive_idempotency_key",
    "get_inventory_level",
    "resolve_location_for_store",
]
