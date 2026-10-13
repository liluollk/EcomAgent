"""Listing 发布能力的 Gateway Port 与命令值对象(P1 Task 8;设计 §4.5、§5.3、§6)。

业务模块拥有自己的 Gateway Port:平台差异只存在于 ``adapters`` 包
(``adapters → agents`` 方向),本模块**不 import adapters**。Adapter 实现
:class:`ListingGateway`,组合根把它注入 Handler 与 Reconciler。

写入沿用 Runtime 的写回执状态契约(:class:`WriteReceipt`):``APPLIED`` /
``NOT_APPLIED`` / ``UNKNOWN`` / ``FAILED``。刊登变更的观测只有"是否真的改变了
状态"与"变更后的资源版本",目标状态在 prepare 阶段已由服务端固定,因此不需要
价格/库存那样的前后值快照——``changed=False`` 表达同目标幂等 no-op。
``UNKNOWN`` 一律不带观测状态,只能交给回查。``idempotency_key`` 由 Runtime
分配的稳定 ``operation_id`` 确定性派生,模型与 Tool 输入都不能提供。

**方向不由命令承载**::class:`ChangePublicationCommand` 不携带目标状态,发布 /
取消发布分别由 :meth:`ListingGateway.publish_listing` 与
:meth:`ListingGateway.unpublish_listing` 表达。这样命令自身不可能与方法指向
不一致的目标,调用方也就无法构造"方法说发布、命令说取消发布"这种自相矛盾的
写入。

平台侧权威刊登快照命名为 :class:`ListingPublicationSnapshot`,与模型可见的
:class:`~agents.product_operations.listing.models.ListingSnapshot` 区分:前者是
Port 的只读观测(只有发布状态与版本),后者是 Tool 的模型可见输出(含标题、价格、
issues),两者不能互相替代。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Protocol

from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.shared.write_contracts import WriteReceipt
from runtime.execution.reconciliation import ReconcileResult


REASON_LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
"""平台侧不存在该 Listing(或跨 Workspace 不可见)时的写回执原因码。"""


class ListingUnavailable(LookupError):
    """平台刊登读取失败:网络错误、非 2xx 或响应不完整。

    读失败不改状态,调用方不应据此推断任何写入结果;与写回执的 ``UNKNOWN``
    语义严格区分。
    """


@dataclass(frozen=True)
class ListingQuery:
    """平台刊登读取查询;内部标识与外部引用均由服务端补全。"""

    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str


@dataclass(frozen=True)
class ListingPublicationSnapshot:
    """平台侧权威刊登发布状态快照(只读观测,不含任何写入意图)。"""

    publication_state: ListingStatus
    listing_version: int
    external_ref: str


@dataclass(frozen=True)
class ChangePublicationCommand:
    """刊登发布状态变更命令(服务端补全后的可信事实)。

    模型可见输入只有 ``store_ref``/``sku`` 与(取消发布时的可选)``reason``;
    ``operation_id``、Listing 标识、外部引用与 ``expected_version`` 由服务端解析
    写入。``idempotency_key`` 由 Runtime 的执行边界注入(来自已持久化的
    Operation),不从 Tool input 读取、也不由模型生成。

    ``reason`` 属**业务语义**:它是业务意图指纹的一部分,不能排除——否则同一个
    ``operation_id`` 换一个下架原因会被错误当成同一个业务动作。
    """

    operation_id: str
    idempotency_key: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    sku: str
    reason: str | None
    expected_version: int


def derive_idempotency_key(operation_id: str) -> str:
    """刊登发布状态变更的规范幂等键派生(供 Runtime/组合根使用)。"""
    return f"change_listing_publication:{operation_id}"


@dataclass(frozen=True)
class ReconcileListingCommand:
    """单个 UNKNOWN 刊登变更操作的不可变平台回查上下文。

    只能由 ``Operation.expected_state`` 构造:不得从最新对话、当前 Agent
    Prompt 或模型文本恢复目标发布状态(设计 §6)。
    """

    operation_id: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    expected_state: ListingStatus


class ListingGateway(Protocol):
    """刊登能力的平台 Port:读刊登、发布、取消发布与回查。"""

    async def get_listing(self, query: ListingQuery) -> ListingPublicationSnapshot:
        """读取平台侧权威发布状态与资源版本。"""
        ...

    async def publish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        """把刊登置为 ``PUBLISHED``;超时或响应丢失返回 UNKNOWN,由调用方回查。"""
        ...

    async def unpublish_listing(self, command: ChangePublicationCommand) -> WriteReceipt:
        """把刊登置为 ``UNPUBLISHED``(保留对象与历史,不清零库存)。"""
        ...

    async def reconcile_listing(self, command: ReconcileListingCommand) -> ReconcileResult:
        """回查刊登变更是否生效;只能返回 APPLIED/NOT_APPLIED/UNKNOWN。"""
        ...


__all__ = [
    "REASON_LISTING_NOT_FOUND",
    "ChangePublicationCommand",
    "ListingGateway",
    "ListingPublicationSnapshot",
    "ListingQuery",
    "ListingUnavailable",
    "ReconcileListingCommand",
    "WriteReceipt",
    "derive_idempotency_key",
]
