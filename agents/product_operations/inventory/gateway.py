"""Inventory 能力的 Gateway Port 与命令值对象(P1 Task 7;设计 §5.3、§6)。

业务模块拥有自己的 Gateway Port:平台差异只存在于 ``adapters`` 包
(``adapters → agents`` 方向),本模块**不 import adapters**。Adapter 实现
:class:`InventoryGateway`,组合根把它注入 Handler 与 Reconciler。

写入沿用 Runtime 的写回执状态契约(:class:`WriteStatus`):``APPLIED`` /
``NOT_APPLIED`` / ``UNKNOWN`` / ``FAILED``;:class:`InventoryWriteReceipt` 在该
状态契约之上补充库存观测(前后数量与最终版本),供模型可见回执与审计使用。
``UNKNOWN`` 一律不带观测状态,只能交给回查。``idempotency_key`` 由 Runtime
分配的稳定 ``operation_id`` 确定性派生,模型与 Tool 输入都不能提供。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Protocol

from agents.product_operations.shared.write_contracts import WriteStatus
from runtime.execution.reconciliation import ReconcileResult


REASON_INVENTORY_NOT_FOUND: Final = "INVENTORY_NOT_FOUND"
"""平台侧不存在该 (位置, 商品) 的库存记录。"""


class InventoryUnavailable(LookupError):
    """平台库存读取失败:网络错误、非 2xx 或响应不完整。

    读失败不改状态,调用方不应据此推断任何写入结果;与写回执的 ``UNKNOWN``
    语义严格区分。
    """


@dataclass(frozen=True)
class InventoryQuery:
    """平台库存读取查询;内部标识与外部引用均由服务端补全。"""

    workspace_id: uuid.UUID
    store_id: uuid.UUID
    location_id: uuid.UUID
    product_id: uuid.UUID
    external_store_ref: str
    external_location_ref: str
    external_product_ref: str


@dataclass(frozen=True)
class InventoryLevelSnapshot:
    """平台侧权威库存快照(只读观测,不含任何写入意图)。"""

    available_quantity: int
    inventory_version: int
    external_ref: str


@dataclass(frozen=True)
class InventoryWriteReceipt:
    """库存写回执:共享状态契约 + 库存观测。

    ``status`` 取 :class:`WriteStatus`;``resource_version`` 是 CAS 写入后的权威
    库存版本。``UNKNOWN`` 时不得携带任何观测(前后数量、版本、外部引用、
    ``changed``)——调用方只能调度回查,不得臆测结果。
    """

    operation_id: str
    idempotency_key: str
    status: WriteStatus
    before_quantity: int | None = None
    after_quantity: int | None = None
    resource_version: int | None = None
    external_ref: str | None = None
    changed: bool | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is WriteStatus.UNKNOWN and any(
            value is not None
            for value in (
                self.before_quantity,
                self.after_quantity,
                self.resource_version,
                self.external_ref,
                self.changed,
            )
        ):
            raise ValueError("UNKNOWN receipt cannot contain observed state")


@dataclass(frozen=True)
class AdjustInventoryCommand:
    """``adjust_inventory_quantity`` 平台写命令(服务端补全后的可信事实)。

    模型可见输入只有 ``store_ref``/``sku``/``location_ref``/``delta``/``reason``;
    ``operation_id``、位置与商品标识、``expected_version``、``quantity_before`` 与
    ``expected_quantity_after`` 由服务端解析写入。``idempotency_key`` 由 Runtime 的
    执行边界注入(来自已持久化的 Operation),不从 Tool input 读取、也不由模型生成。
    """

    operation_id: str
    idempotency_key: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    location_id: uuid.UUID
    product_id: uuid.UUID
    external_store_ref: str
    external_location_ref: str
    external_product_ref: str
    sku: str
    delta: int
    reason: str
    expected_version: int
    quantity_before: int

    @property
    def expected_quantity_after(self) -> int:
        """由写前持久化的 ``quantity_before`` 与 ``delta`` 确定的结果数量。"""
        return self.quantity_before + self.delta


def derive_idempotency_key(operation_id: str) -> str:
    """``adjust_inventory_quantity`` 的规范幂等键派生(供 Runtime/组合根使用)。"""
    return f"adjust_inventory_quantity:{operation_id}"


@dataclass(frozen=True)
class ReconcileInventoryCommand:
    """单个 UNKNOWN 库存调整操作的不可变平台回查上下文。

    只能由 ``Operation.expected_state`` 构造:不得从最新对话、当前 Agent
    Prompt 或模型文本恢复目标数量(设计 §6)。
    """

    operation_id: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    location_id: uuid.UUID
    product_id: uuid.UUID
    external_store_ref: str
    external_location_ref: str
    external_product_ref: str
    quantity_before: int
    expected_quantity_after: int


class InventoryGateway(Protocol):
    """库存能力的平台 Port:读库存、调库存与回查。"""

    async def get_inventory(self, query: InventoryQuery) -> InventoryLevelSnapshot:
        """读取平台侧权威库存数量与资源版本。"""
        ...

    async def adjust_inventory(
        self, command: AdjustInventoryCommand
    ) -> InventoryWriteReceipt:
        """执行增量调整;超时或响应丢失返回 UNKNOWN,由调用方回查。"""
        ...

    async def reconcile_inventory(
        self, command: ReconcileInventoryCommand
    ) -> ReconcileResult:
        """回查库存调整是否生效;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。"""
        ...


__all__ = [
    "REASON_INVENTORY_NOT_FOUND",
    "AdjustInventoryCommand",
    "InventoryGateway",
    "InventoryLevelSnapshot",
    "InventoryQuery",
    "InventoryUnavailable",
    "InventoryWriteReceipt",
    "ReconcileInventoryCommand",
    "derive_idempotency_key",
]
