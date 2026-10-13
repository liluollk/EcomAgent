"""商品模块的规范错误码与错误基类(P1 Task 3)。

错误码是跨模块契约的一部分:模型可见结果、审计记录与评测断言都只认这些
字符串,因此它们必须稳定、与文案解耦,且不允许各模块各自发明同义码。

语义约束:

- ``AUTH_EXPIRED`` / ``RATE_LIMITED`` / ``PLATFORM_UNAVAILABLE``:平台侧可重试
  或需重新授权的暂时性失败,不得据此断定写入未发生;
- ``CAPABILITY_UNSUPPORTED``:当前平台适配边界不支持该能力(例如 P1 的 REST
  Adapter 只声明定价能力);不得降级为"删除资源"等替代动作;
- ``VERSION_CONFLICT``:资源版本已变化,必须在重新读取后再决策;
- ``OUTCOME_UNKNOWN``:无法判定外部副作用是否发生,只能交给回查;
- ``AMBIGUOUS_LOCATION``:履约位置无法确定性解析,需要用户补充输入;
- ``AMBIGUOUS_STORE_REFERENCE``:店铺引用(名称/别名)匹配到多个店铺,无法
  确定性解析,需要用户补充输入;
- ``INVALID_STATE_TRANSITION``:业务状态机不允许该迁移。
"""

from __future__ import annotations

from typing import Any, Final


AUTH_EXPIRED: Final = "AUTH_EXPIRED"
RATE_LIMITED: Final = "RATE_LIMITED"
CAPABILITY_UNSUPPORTED: Final = "CAPABILITY_UNSUPPORTED"
NOT_FOUND: Final = "NOT_FOUND"
VALIDATION_FAILED: Final = "VALIDATION_FAILED"
VERSION_CONFLICT: Final = "VERSION_CONFLICT"
PLATFORM_UNAVAILABLE: Final = "PLATFORM_UNAVAILABLE"
OUTCOME_UNKNOWN: Final = "OUTCOME_UNKNOWN"
AMBIGUOUS_LOCATION: Final = "AMBIGUOUS_LOCATION"
AMBIGUOUS_STORE_REFERENCE: Final = "AMBIGUOUS_STORE_REFERENCE"
INVALID_STATE_TRANSITION: Final = "INVALID_STATE_TRANSITION"


class ProductOperationError(Exception):
    """商品模块的规范错误:携带稳定 ``code`` 与可选的诊断细节。"""

    def __init__(self, code: str, detail: str = "", **context: Any) -> None:
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.context: dict[str, Any] = dict(context)


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
    "ProductOperationError",
]
