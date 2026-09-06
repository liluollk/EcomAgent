"""
错误分类体系 — 将上游错误分为 CLIENT_ERROR / BUSINESS_ERROR / TRANSIENT_ERROR / FATAL_ERROR。

用于 RetryPolicy 判定是否可重试：
  CLIENT_ERROR   → 不重试（参数/鉴权问题，重试无意义）
  BUSINESS_ERROR → 不重试（业务规则冲突，重试不会改变结果）
  TRANSIENT_ERROR → 可重试（临时故障，稍后可能恢复）
  FATAL_ERROR    → 默认不重试（内部错误，可能引发更严重问题）
"""

from __future__ import annotations

import asyncio
from enum import Enum


class ErrorCategory(Enum):
    CLIENT_ERROR = "CLIENT_ERROR"
    BUSINESS_ERROR = "BUSINESS_ERROR"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    FATAL_ERROR = "FATAL_ERROR"


_HTTP_STATUS_CATEGORY: dict[int, ErrorCategory] = {
    400: ErrorCategory.CLIENT_ERROR,
    401: ErrorCategory.CLIENT_ERROR,
    403: ErrorCategory.CLIENT_ERROR,
    404: ErrorCategory.CLIENT_ERROR,
    409: ErrorCategory.BUSINESS_ERROR,
    422: ErrorCategory.CLIENT_ERROR,
    429: ErrorCategory.TRANSIENT_ERROR,
    500: ErrorCategory.FATAL_ERROR,
    502: ErrorCategory.TRANSIENT_ERROR,
    503: ErrorCategory.TRANSIENT_ERROR,
    504: ErrorCategory.TRANSIENT_ERROR,
}

_PLATFORM_CODE_CATEGORY: dict[int, ErrorCategory] = {
    10001: ErrorCategory.CLIENT_ERROR,
    10002: ErrorCategory.CLIENT_ERROR,
    10003: ErrorCategory.CLIENT_ERROR,
    10004: ErrorCategory.BUSINESS_ERROR,
    10005: ErrorCategory.TRANSIENT_ERROR,
}


def classify_http_status(status_code: int) -> ErrorCategory:
    """根据 HTTP 状态码分类错误。"""
    return _HTTP_STATUS_CATEGORY.get(status_code, ErrorCategory.FATAL_ERROR)


def classify_platform_code(code: int) -> ErrorCategory:
    """根据平台业务错误码分类错误。"""
    return _PLATFORM_CODE_CATEGORY.get(code, ErrorCategory.FATAL_ERROR)


def is_retryable(category: ErrorCategory) -> bool:
    """判断给定错误分类是否可重试。"""
    return category == ErrorCategory.TRANSIENT_ERROR


def classify_exception(exc: BaseException) -> ErrorCategory:
    """把执行期异常归入四类错误。

    用鸭子类型识别上游 API 异常（带整型 code 属性，如 RestApiError），
    避免 execution 层反向依赖 integrations 层；其余按异常类型判定：
      - 超时（含 asyncio.TimeoutError）→ TRANSIENT_ERROR
      - 其余带平台/HTTP 错误码的异常按码表分类
      - 未知异常 → FATAL_ERROR（默认不重试）
    """
    from .policy.timeout import TimeoutError as ToolTimeoutError

    if isinstance(exc, (ToolTimeoutError, asyncio.TimeoutError)):
        return ErrorCategory.TRANSIENT_ERROR
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        # 平台码段（10001-10005/99999）与 HTTP 状态码取值不重叠，先查平台码表
        category = _PLATFORM_CODE_CATEGORY.get(code)
        if category is not None:
            return category
        return _HTTP_STATUS_CATEGORY.get(code, ErrorCategory.FATAL_ERROR)
    return ErrorCategory.FATAL_ERROR