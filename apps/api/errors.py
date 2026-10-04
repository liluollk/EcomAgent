"""统一 API 错误模型(计划 04 Task 1;架构文档 §2)。

错误响应体统一为 ``{"code", "message", "request_id"}``,不携带异常堆栈;
``request_id`` 由 ``RequestContextMiddleware`` 每请求确定并可从
``X-Request-Id`` 透传。稳定错误码集中在本模块,供路由与测试引用。
"""

from typing import Final


UNAUTHENTICATED: Final = "UNAUTHENTICATED"
"""缺少或非 Bearer 的访问凭证。"""

TOKEN_INVALID: Final = "TOKEN_INVALID"
"""JWT 签名无效、算法不符、已过期或 claim 不合规。"""

ACTOR_UNAVAILABLE: Final = "ACTOR_UNAVAILABLE"
"""JWT 有效但账号或成员身份在库中缺失、禁用,无法构建可信 ActorContext。"""

INVALID_CREDENTIALS: Final = "INVALID_CREDENTIALS"
"""登录名或密码不正确;不区分账号是否存在。"""

PERMISSION_DENIED: Final = "PERMISSION_DENIED"
"""身份可信但当前库态下无任何可用授权。"""

LAST_OWNER_REQUIRED: Final = "LAST_OWNER_REQUIRED"
"""移除或降级最后一个 WorkspaceOwner 被拒绝(409)。"""

CONFLICT: Final = "CONFLICT"
"""与 Workspace 内已有配置冲突,如重复 stable_key 或成员已存在(409)。"""

VALIDATION_ERROR: Final = "VALIDATION_ERROR"
"""请求体或参数未通过校验。"""

NOT_FOUND: Final = "NOT_FOUND"
METHOD_NOT_ALLOWED: Final = "METHOD_NOT_ALLOWED"
HTTP_ERROR: Final = "HTTP_ERROR"
"""框架级 HTTP 异常(404/405 之外的其余状态码)。"""

INTERNAL_ERROR: Final = "INTERNAL_ERROR"
"""未处理异常;对外只返回固定文案,不泄漏堆栈。"""


class ApiError(Exception):
    """携带稳定错误码的异常,由全局异常处理器渲染为统一错误体。"""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
