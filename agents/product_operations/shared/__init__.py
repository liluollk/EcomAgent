"""商品模块跨能力复用的有限契约(架构文档 §3、§6)。

只放真正跨能力共享的内容:规范错误码、写回执不变量、Reconciler 分发注册表。
本包不演化为万能工具箱:价格、库存、刊登各自的模型、Policy、Gateway 与
Reconciler 仍留在自己的模块里。
"""

from __future__ import annotations

from agents.product_operations.shared.errors import (
    AMBIGUOUS_LOCATION,
    AMBIGUOUS_STORE_REFERENCE,
    AUTH_EXPIRED,
    CAPABILITY_UNSUPPORTED,
    INVALID_STATE_TRANSITION,
    NOT_FOUND,
    OUTCOME_UNKNOWN,
    PLATFORM_UNAVAILABLE,
    RATE_LIMITED,
    VALIDATION_FAILED,
    VERSION_CONFLICT,
    ProductOperationError,
)
from agents.product_operations.shared.reconciliation import (
    OperationReconcilerRegistry,
    ProductOperationReconciler,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus


__all__ = [
    "AMBIGUOUS_LOCATION",
    "AMBIGUOUS_STORE_REFERENCE",
    "AUTH_EXPIRED",
    "CAPABILITY_UNSUPPORTED",
    "INVALID_STATE_TRANSITION",
    "NOT_FOUND",
    "OUTCOME_UNKNOWN",
    "PLATFORM_UNAVAILABLE",
    "RATE_LIMITED",
    "VALIDATION_FAILED",
    "VERSION_CONFLICT",
    "OperationReconcilerRegistry",
    "ProductOperationError",
    "ProductOperationReconciler",
    "WriteReceipt",
    "WriteStatus",
]
