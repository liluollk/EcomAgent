"""平台协议与命令/回执值对象(计划 03 Task 5;架构文档 §4.4、§7)。

导入方向(架构文档 §2):平台协议由 Agent/Adapter 侧实现,Runtime 不感知
平台类型——本模块可 import runtime/agents,反向禁止。``ReconcileResult``
直接复用 ``runtime.execution.reconciliation`` 的既有枚举,保证回查结论与
Runtime 的 Reconciliation 语义(APPLIED/NOT_APPLIED/UNKNOWN)一一对应。

``CommerceAdapter`` 为结构化 Protocol:上层(商品 Agent/组合根)依赖协议
注入具体平台实现;Mock 实现见 ``adapters.commerce.mock_adapter``。
"""

import enum
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

from runtime.execution.reconciliation import ReconcileResult


class ExecutionStatus(str, enum.Enum):
    """``execute`` 回执状态(架构文档 §7:超时或响应丢失返回 UNKNOWN)。

    - ``APPLIED``:写入已生效;
    - ``NOT_APPLIED``:命令确定未被应用;
    - ``UNKNOWN``:写入是否生效无法确认,必须调度 ``reconcile`` 回查;
    - ``FAILED``:确定失败(如版本冲突、命令校验拒绝),不得当作 UNKNOWN。
    """

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


REASON_VERSION_CONFLICT: Final = "VERSION_CONFLICT"
REASON_LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
REASON_CURRENCY_MISMATCH: Final = "CURRENCY_MISMATCH"
REASON_PLATFORM_FAILURE: Final = "PLATFORM_FAILURE"
REASON_TIMEOUT_BEFORE_WRITE: Final = "TIMEOUT_BEFORE_WRITE"
REASON_RESPONSE_LOST_AFTER_COMMIT: Final = "RESPONSE_LOST_AFTER_COMMIT"
"""回执 ``reason_code``:不参与状态迁移,供 Runtime 记录与诊断。"""

RETRYABLE_REASON_CODES: Final[frozenset[str]] = frozenset()
"""可安全重试的确定失败原因;当前为空。

"确定失败"只说明这次没生效,不说明再试一次值得:同一 ``operation_id`` /
``expected_version`` 下,任何确定失败重试都只会得到同一回执。

- ``PLATFORM_FAILURE`` 由持续不可用(``permanent_failure``)产生,重试仍会失败;
- ``VERSION_CONFLICT`` 不能在同一 ``operation_id`` 上改写 ``expected_version``,
  且平台已按该 ``operation_id`` 记录失败回执、重复提交只会回放旧结论。正确做法
  是把失败返回模型,重新读取资源后创建**新的** ToolCall/Operation(必要时重新确认)。

因此当前集合为空:任何确定失败都落 ToolCall 终态。若将来需要自动重试,应新增
独立原因码(如 ``PLATFORM_UNAVAILABLE`` / ``RATE_LIMITED``)并以持久化的重试预算
与退避约束,而不是复用这些"重试必然同结果"的码值。
"""


def is_retryable_reason_code(reason_code: str | None) -> bool:
    """确定性失败是否允许在重新通过完整 PreToolUse 后再次执行。"""
    return reason_code in RETRYABLE_REASON_CODES


@dataclass(frozen=True)
class UpdatePriceCommand:
    """``update_price`` 写命令(架构文档 §4.4 服务端补全字段;§7 签名)。

    模型可见输入只有 ``store_id``/``sku``/``target_price``;``operation_id``、
    Listing 标识、``currency`` 与 ``expected_version`` 由服务端补全后进入
    Adapter,幂等键由 ``operation_id`` 确定性派生。
    """

    operation_id: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    target_price: Decimal
    currency: str
    expected_version: int

    @property
    def idempotency_key(self) -> str:
        """幂等键:Runtime 分配的稳定 operation_id 的确定性派生。"""
        return f"update_price:{self.operation_id}"


@dataclass(frozen=True)
class ExecutionReceipt:
    """``execute`` 回执(架构文档 §4.4 输出:前后价格、最终版本、外部引用)。

    ``status`` 为 UNKNOWN 时观测字段一律为 ``None``——调用方未收到响应,
    不得从回执臆测写入结果,只能调度 ``reconcile``。

    ``retryable`` 是执行端对"这次失败是否值得重试"的显式声明,默认 False;
    Runtime 只有在它为 True 时才允许该 ToolCall 重走完整 PreToolUse 后再次执行。
    """

    operation_id: str
    idempotency_key: str
    status: ExecutionStatus
    before_price: Decimal | None = None
    after_price: Decimal | None = None
    listing_version: int | None = None
    external_ref: str | None = None
    reason_code: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class ReconciliationCommand:
    """单个 UNKNOWN 操作的不可变平台回查上下文。"""

    operation_id: str
    workspace_id: uuid.UUID
    store_id: uuid.UUID
    listing_id: uuid.UUID
    external_store_ref: str
    external_product_ref: str
    expected_price: Decimal
    currency: str


class CommerceAdapter(Protocol):
    """平台 Adapter 协议(架构文档 §7:execute + reconcile)。"""

    async def execute(self, command: UpdatePriceCommand) -> ExecutionReceipt:
        """执行写命令;超时或响应丢失返回 UNKNOWN,由调用方调度回查。"""
        ...

    async def reconcile(
        self, command: ReconciliationCommand
    ) -> ReconcileResult:
        """回查:只能返回 APPLIED/NOT_APPLIED/UNKNOWN,不以模型文本判定。"""
        ...

__all__ = [
    "REASON_CURRENCY_MISMATCH",
    "REASON_LISTING_NOT_FOUND",
    "REASON_PLATFORM_FAILURE",
    "REASON_RESPONSE_LOST_AFTER_COMMIT",
    "REASON_TIMEOUT_BEFORE_WRITE",
    "REASON_VERSION_CONFLICT",
    "RETRYABLE_REASON_CODES",
    "CommerceAdapter",
    "ExecutionReceipt",
    "ExecutionStatus",
    "ReconcileResult",
    "ReconciliationCommand",
    "UpdatePriceCommand",
    "is_retryable_reason_code",
]
