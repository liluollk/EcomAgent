"""商品模块的写回执契约(P1 Task 3;架构文档 §6)。

写回执是 Gateway 与 Runtime 之间的唯一事实通道:

- ``APPLIED`` / ``NOT_APPLIED``:结果确定,可以据此结束或重试(重试必须重走
  完整 PreToolUse);
- ``UNKNOWN``:结果不确定,只能交给回查 —— 因此禁止携带任何观测到的状态
  (资源版本、外部引用、是否变更),否则回查会拿到未被证据支持的伪事实;
- ``FAILED``:确定性失败,由 Runtime 的分类规则落为 ``NOT_APPLIED``。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class WriteStatus(str, enum.Enum):
    """写回执状态;与 Runtime 的 ``APPLIED | NOT_APPLIED | UNKNOWN`` 分类对齐。"""

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WriteReceipt:
    """平台写回执。

    ``resource_version`` 是该资源的版本(CAS 写入后的权威版本);
    ``external_ref`` 是平台侧引用;``changed`` 表达本次调用是否真的改变了平台
    状态(幂等 no-op 时为 False);``reason_code`` 使用
    :mod:`agents.product_operations.shared.errors` 中的规范错误码。
    """

    operation_id: str
    idempotency_key: str
    status: WriteStatus
    resource_version: int | None = None
    external_ref: str | None = None
    changed: bool | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is WriteStatus.UNKNOWN and any(
            value is not None
            for value in (self.resource_version, self.external_ref, self.changed)
        ):
            raise ValueError("UNKNOWN receipt cannot contain observed state")


__all__ = ["WriteReceipt", "WriteStatus"]
