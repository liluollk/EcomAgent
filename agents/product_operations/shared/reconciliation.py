"""商品 Operation 的回查分发(P1 Task 3;架构文档 §6)。

Runtime 只给出 ``Operation.expected_state`` 与三态回查结论(APPLIED /
NOT_APPLIED / UNKNOWN);把某个 ``operation_type`` 映射到具体 Reconciler 是
商品应用层的职责:

- 分发键来自 prepare 阶段写入的受控 ``operation_type``,**不是**模型参数,
  因此模型无法选择或影响回查路径;
- 未注册类型一律拒绝,不做"默认回查"猜测。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from runtime.execution.reconciliation import ReconcileResult


class ProductOperationReconciler(Protocol):
    """单个 ``operation_type`` 的回查端点。

    只允许使用 ``expected_state`` 构造平台查询:不得从最新对话、当前 Agent
    Prompt 或模型文本恢复目标值。
    """

    async def reconcile(self, expected_state: Mapping[str, Any]) -> ReconcileResult: ...


class OperationReconcilerRegistry:
    """``operation_type`` → Reconciler 的确定性注册表。"""

    def __init__(self) -> None:
        self._by_operation_type: dict[str, ProductOperationReconciler] = {}

    def register(
        self, operation_type: str, reconciler: ProductOperationReconciler
    ) -> None:
        if operation_type in self._by_operation_type:
            raise ValueError(f"duplicate reconciler: {operation_type}")
        self._by_operation_type[operation_type] = reconciler

    def require(self, operation_type: str) -> ProductOperationReconciler:
        try:
            return self._by_operation_type[operation_type]
        except KeyError as exc:
            raise ValueError(f"unregistered operation type: {operation_type}") from exc

    def operation_types(self) -> tuple[str, ...]:
        return tuple(self._by_operation_type)


__all__ = ["OperationReconcilerRegistry", "ProductOperationReconciler"]
