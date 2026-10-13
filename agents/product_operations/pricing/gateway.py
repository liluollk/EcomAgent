"""Pricing 能力的 Gateway Port 与命令值对象(P1 Task 6;设计 §5.3、§6)。

业务模块拥有自己的 Gateway Port:平台差异只存在于 ``adapters`` 包
(``adapters → agents`` 方向),本模块**不 import adapters**。Adapter 实现
:class:`PricingGateway`,组合根把它注入 Handler 与 Reconciler。

写入沿用 Runtime 的写回执状态契约(:class:`WriteStatus`):``APPLIED`` /
``NOT_APPLIED`` / ``UNKNOWN`` / ``FAILED``;:class:`PriceWriteReceipt` 在该状态
契约之上补充价格观测(前后价格与最终 Listing 版本),供模型可见回执与审计使用。
``UNKNOWN`` 一律不带观测状态,只能交给回查。``idempotency_key`` 由 Runtime
分配的稳定 ``operation_id`` 确定性派生,模型与 Tool 输入都不能提供。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from agents.product_operations.shared.write_contracts import WriteStatus
from runtime.execution.reconciliation import ReconcileResult


class PriceUnavailable(LookupError):
    """平台价格读取失败:网络错误、非 2xx 或响应不完整。

    读失败不改状态,调用方不应据此推断任何写入结果;与写回执的 ``UNKNOWN``
    语义严格区分。
    """


@dataclass(frozen=True)
class PriceQuery:
    """平台价格读取查询;内部标识与外部引用均由服务端补全。"""

    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    currency: str


@dataclass(frozen=True)
class PriceSnapshot:
    """平台侧权威价格快照(只读观测,不含任何写入意图)。"""

    price: Decimal
    currency: str
    listing_version: int
    external_ref: str


@dataclass(frozen=True)
class PriceWriteReceipt:
    """价格写回执:共享状态契约 + 价格观测。

    ``status`` 取 :class:`WriteStatus`;``resource_version`` 是 CAS 写入后的权威
    Listing 版本。``UNKNOWN`` 时不得携带任何观测(价格、版本、外部引用、
    ``changed``)——调用方只能调度回查,不得臆测结果。
    """

    operation_id: str
    idempotency_key: str
    status: WriteStatus
    before_price: Decimal | None = None
    after_price: Decimal | None = None
    resource_version: int | None = None
    external_ref: str | None = None
    changed: bool | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is WriteStatus.UNKNOWN and any(
            value is not None
            for value in (
                self.before_price,
                self.after_price,
                self.resource_version,
                self.external_ref,
                self.changed,
            )
        ):
            raise ValueError("UNKNOWN receipt cannot contain observed state")


@dataclass(frozen=True)
class SetListingPriceCommand:
    """``set_listing_price`` 平台写命令(服务端补全后的可信事实)。

    模型可见输入只有 ``store_ref``/``sku``/``target_price``;``operation_id``、
    Listing 标识、``currency`` 与 ``expected_version`` 由服务端解析写入。
    ``idempotency_key`` 由 Runtime 的执行边界注入(来自已持久化的 Operation),
    不从 Tool input 读取、也不由模型生成。
    """

    operation_id: str
    idempotency_key: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    sku: str
    target_price: Decimal
    currency: str
    expected_version: int


def derive_idempotency_key(operation_id: str) -> str:
    """``set_listing_price`` 的规范幂等键派生(供 Runtime/组合根使用)。"""
    return f"set_listing_price:{operation_id}"


@dataclass(frozen=True)
class ReconcilePriceCommand:
    """单个 UNKNOWN 改价操作的不可变平台回查上下文。

    只能由 ``Operation.expected_state`` 构造:不得从最新对话、当前 Agent
    Prompt 或模型文本恢复目标价格(设计 §6)。
    """

    operation_id: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    expected_price: Decimal
    currency: str


class PricingGateway(Protocol):
    """价格能力的平台 Port:读价、写价与回查。"""

    async def get_price(self, query: PriceQuery) -> PriceSnapshot:
        """读取平台侧权威价格与资源版本。"""
        ...

    async def set_price(self, command: SetListingPriceCommand) -> PriceWriteReceipt:
        """执行绝对目标价写入;超时或响应丢失返回 UNKNOWN,由调用方回查。"""
        ...

    async def reconcile_price(self, command: ReconcilePriceCommand) -> ReconcileResult:
        """回查改价是否生效;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。"""
        ...


__all__ = [
    "PriceQuery",
    "PriceSnapshot",
    "PriceUnavailable",
    "PriceWriteReceipt",
    "PricingGateway",
    "ReconcilePriceCommand",
    "SetListingPriceCommand",
    "derive_idempotency_key",
]
