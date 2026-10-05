"""API 应用服务共享错误基类。"""


class ApplicationServiceError(Exception):
    """携带稳定 HTTP 状态码与错误码的应用服务错误。"""

    status_code: int = 500
    code: str = "APPLICATION_SERVICE_ERROR"
