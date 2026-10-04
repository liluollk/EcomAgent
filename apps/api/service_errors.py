"""WorkspaceService 领域错误到统一 API 错误体的翻译(计划 04 Task 2)。

应用服务只抛领域错误并携带稳定状态码/错误码;路由层通过 ``translated()``
把领域错误翻译为 :class:`apps.api.errors.ApiError`,统一错误体仍由全局
异常处理器渲染。
"""

from collections.abc import Iterator
from contextlib import contextmanager

from apps.api.errors import ApiError
from runtime.workspace.service import WorkspaceServiceError


@contextmanager
def translated() -> Iterator[None]:
    """把 WorkspaceServiceError 翻译为 ApiError;其余异常原样穿透。"""
    try:
        yield
    except WorkspaceServiceError as error:
        raise ApiError(
            status_code=error.status_code, code=error.code, message=str(error)
        ) from error
