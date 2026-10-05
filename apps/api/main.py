"""FastAPI 组合根(计划 04 Task 1)。

``create_app()`` 装配路由、请求标识中间件与统一异常处理器;数据库引擎在
lifespan 中从 ``RUNTIME_DATABASE_URL`` 创建并挂到 ``app.state``,测试可直接
注入既有引擎覆盖。错误响应体统一为 ``{"code", "message", "request_id"}``,
不返回异常堆栈。
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apps.api.dependencies import REQUEST_ID_HEADER, get_request_id
from apps.api.errors import (
    HTTP_ERROR,
    INTERNAL_ERROR,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    VALIDATION_ERROR,
    ApiError,
)
from apps.api.routes.agents import router as agents_router
from apps.api.routes.auth import router as auth_router
from apps.api.routes.configuration import router as configuration_router
from apps.api.routes.events import router as events_router
from apps.api.routes.health import router as health_router
from apps.api.routes.memberships import router as memberships_router
from apps.api.routes.memories import router as memories_router
from apps.api.routes.requests import router as requests_router
from apps.api.routes.runs import router as runs_router
from apps.api.routes.sessions import router as sessions_router
from apps.api.routes.workspaces import router as workspaces_router
from runtime.persistence.database import create_engine, get_database_url


_FRAMEWORK_ERROR_CODES: dict[int, str] = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}

_E2E_ENABLED_VALUES: Final = frozenset({"1", "true", "yes", "on"})


def _error_payload(request: Request, code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "request_id": get_request_id(request)}


async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(request, exc.code, exc.message),
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=_error_payload(request, VALIDATION_ERROR, "请求体或参数不合法"),
    )


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    code = _FRAMEWORK_ERROR_CODES.get(exc.status_code, HTTP_ERROR)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(request, code, str(exc.detail)),
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 对外固定文案;堆栈只留在服务端日志,不进入响应体。
    return JSONResponse(
        status_code=500,
        content=_error_payload(request, INTERNAL_ERROR, "服务器内部错误"),
    )


class RequestContextMiddleware:
    """纯 ASGI 中间件:每请求确定 request_id,写入 scope.state 与响应头。

    request_id 优先透传请求头 ``X-Request-Id``,否则随机生成;同一请求内的
    依赖、路由与异常处理器通过 ``request.state`` 读到同一值。
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        request_id_header = REQUEST_ID_HEADER.lower().encode("latin-1")
        for raw_name, raw_value in scope.get("headers") or []:
            if raw_name.lower() == request_id_header:
                request_id = raw_value.decode("latin-1")
                break
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers") or [])
                response_headers.append((request_id_header, request_id.encode("latin-1")))
                message = {**message, "headers": response_headers}
            await send(message)

        await self._app(scope, receive, send_with_request_id)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时确保引擎与会话工厂就绪;自行创建的引擎在关闭时释放。"""
    engine: AsyncEngine | None = getattr(app.state, "engine", None)
    owns_engine = engine is None
    if engine is None:
        engine = create_engine(get_database_url())
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        if owns_engine:
            await engine.dispose()


def create_app(engine: AsyncEngine | None = None) -> FastAPI:
    """构建 FastAPI 应用;测试可注入既有引擎,跳过 lifespan 内的引擎创建。

    ``RUNTIME_E2E=1`` 时追加注册 E2E 种子端点(阶段 4 Gate;仅本地/CI
    端到端验证使用)。
    """
    app = FastAPI(title="Agent Runtime API", lifespan=_lifespan)
    if engine is not None:
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.add_middleware(RequestContextMiddleware)
    app.exception_handler(ApiError)(_api_error_handler)
    app.exception_handler(RequestValidationError)(_validation_error_handler)
    app.exception_handler(StarletteHTTPException)(_http_exception_handler)
    app.exception_handler(Exception)(_unhandled_error_handler)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(agents_router)
    app.include_router(workspaces_router)
    app.include_router(memberships_router)
    app.include_router(configuration_router)
    app.include_router(sessions_router)
    app.include_router(runs_router)
    app.include_router(requests_router)
    app.include_router(events_router)
    app.include_router(memories_router)
    if os.environ.get("RUNTIME_E2E", "").strip().lower() in _E2E_ENABLED_VALUES:
        from apps.api.e2e import router as e2e_router

        app.include_router(e2e_router)
    return app


# uvicorn 入口(uvicorn apps.api.main:app):引擎由 lifespan 从
# RUNTIME_DATABASE_URL 创建;测试/嵌入式场景请直接调用 create_app(engine)。
app = create_app()
