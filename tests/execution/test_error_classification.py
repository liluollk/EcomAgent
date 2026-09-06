"""错误分类测试 — HTTP 状态 / 平台业务码 / 异常类型 → 四类错误。"""

import asyncio

from execution.error_classification import (
    ErrorCategory,
    classify_exception,
    classify_http_status,
    classify_platform_code,
    is_retryable,
)
from execution.policy.timeout import TimeoutError as ToolTimeoutError


def test_http_status_mapping():
    assert classify_http_status(429) is ErrorCategory.TRANSIENT_ERROR
    assert classify_http_status(409) is ErrorCategory.BUSINESS_ERROR
    assert classify_http_status(401) is ErrorCategory.CLIENT_ERROR
    assert classify_http_status(500) is ErrorCategory.FATAL_ERROR
    assert classify_http_status(418) is ErrorCategory.FATAL_ERROR  # 未知状态码


def test_platform_code_mapping():
    assert classify_platform_code(10005) is ErrorCategory.TRANSIENT_ERROR
    assert classify_platform_code(10004) is ErrorCategory.BUSINESS_ERROR
    assert classify_platform_code(10002) is ErrorCategory.CLIENT_ERROR
    assert classify_platform_code(99999) is ErrorCategory.FATAL_ERROR
    assert classify_platform_code(-1) is ErrorCategory.FATAL_ERROR


def test_retryable_only_transient():
    assert is_retryable(ErrorCategory.TRANSIENT_ERROR)
    for c in (ErrorCategory.CLIENT_ERROR, ErrorCategory.BUSINESS_ERROR, ErrorCategory.FATAL_ERROR):
        assert not is_retryable(c)


def test_classify_exception_timeout_is_transient():
    assert classify_exception(ToolTimeoutError("query_inventory", 0.5)) is ErrorCategory.TRANSIENT_ERROR
    assert classify_exception(asyncio.TimeoutError()) is ErrorCategory.TRANSIENT_ERROR


def test_classify_exception_duck_typed_api_error():
    """鸭子类型识别带整型 code 的上游异常（不依赖 integrations 层）。"""

    class FakeApiError(Exception):
        def __init__(self, code: int) -> None:
            super().__init__("x")
            self.code = code

    assert classify_exception(FakeApiError(10005)) is ErrorCategory.TRANSIENT_ERROR
    assert classify_exception(FakeApiError(10004)) is ErrorCategory.BUSINESS_ERROR
    assert classify_exception(FakeApiError(10002)) is ErrorCategory.CLIENT_ERROR
    assert classify_exception(FakeApiError(99999)) is ErrorCategory.FATAL_ERROR


def test_classify_exception_unknown_is_fatal():
    assert classify_exception(ValueError("boom")) is ErrorCategory.FATAL_ERROR
