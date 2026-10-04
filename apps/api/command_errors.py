"""SessionCommandError 到统一 API 错误体的翻译(计划 04 Task 3)。

与 :mod:`apps.api.service_errors` 同构:runtime 侧应用服务只抛领域错误并
携带稳定状态码/错误码,路由层负责翻译为 :class:`apps.api.errors.ApiError`,
统一错误体仍由全局异常处理器渲染。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from apps.api.errors import ApiError
from runtime.workspace.session_commands import SessionCommandError


@contextmanager
def translated_commands() -> Iterator[None]:
    """把 SessionCommandError 翻译为 ApiError;其余异常原样穿透。"""
    try:
        yield
    except SessionCommandError as error:
        raise ApiError(
            status_code=error.status_code, code=error.code, message=str(error)
        ) from error
