"""商品 Operation 的回查分发(P1 Task 3;架构文档 §6)。

Runtime 只给出 ``Operation.expected_state`` 与三态回查结论(APPLIED /
NOT_APPLIED / UNKNOWN);把某个 ``operation_type`` 映射到具体 Reconciler 是
商品应用层的职责:

- 分发键来自 prepare 阶段写入的受控 ``operation_type``,**不是**模型参数,
  因此模型无法选择或影响回查路径;
- 未注册类型一律拒绝,不做"默认回查"猜测。

各能力的 ``build_reconcile_*_command`` 还需把持久化的 ``workspace_id`` 与当前
Reconciler 所属 Workspace 绑定起来——回查必须定位到"写前那个 Workspace 里的那次
操作",不能让错误装配或脏数据把当前 Workspace 覆盖到持久预期上(见
:func:`resolve_persisted_workspace_id`)。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from agents.product_operations.shared.errors import (
    VALIDATION_FAILED,
    ProductOperationError,
)
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


def resolve_persisted_workspace_id(
    expected_state: Mapping[str, Any],
    *,
    current_workspace_id: uuid.UUID,
) -> uuid.UUID:
    """解析并校验 ``expected_state`` 中持久化的 ``workspace_id``。

    回查只能用**写前持久化**的 Workspace 身份定位平台记录,并且它必须与当前
    Reconciler 所属的 ``current_workspace_id`` 一致:

    - 缺失:``VALIDATION_FAILED``(调用方通常已在 ``_REQUIRED_KEYS`` 拦截,此处兜底);
    - 无法解析成 UUID:``VALIDATION_FAILED``;
    - 与当前 Workspace 不一致:``VALIDATION_FAILED``。

    不允许"用当前 Workspace 覆盖持久预期":错误装配或跨 Workspace 脏数据必须
    fail closed,而不是继续拿当前 Workspace 去查平台。

    返回解析出的持久化 Workspace 身份(此时已与当前 Workspace 相等)。
    """
    raw = expected_state.get("workspace_id")
    if raw is None:
        raise ProductOperationError(VALIDATION_FAILED, "expected_state 缺少 workspace_id")
    try:
        persisted_workspace_id = uuid.UUID(str(raw))
    except (ValueError, TypeError) as error:
        raise ProductOperationError(
            VALIDATION_FAILED, "expected_state 中的 workspace_id 无法解析"
        ) from error
    if persisted_workspace_id != current_workspace_id:
        raise ProductOperationError(
            VALIDATION_FAILED,
            "expected_state 的 workspace_id 与当前 Workspace 不一致",
        )
    return persisted_workspace_id


__all__ = [
    "OperationReconcilerRegistry",
    "ProductOperationReconciler",
    "resolve_persisted_workspace_id",
]
