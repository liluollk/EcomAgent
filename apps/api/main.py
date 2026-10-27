"""FastAPI 组合根(生产入口)。

``create_app()`` 装配路由、请求标识中间件与统一异常处理器;数据库引擎在
lifespan 中按 :class:`apps.settings.StartupSettings` 创建并挂到 ``app.state``,
测试可直接注入既有引擎覆盖。装配内容全部由调用方显式传入,不读环境变量决定,
因此本文件只装生产需要的东西:不含种子接口,也没有脚本模型。

错误响应体统一为 ``{"code", "message", "request_id"}``,不返回异常堆栈。
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
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
from apps.api.executor_host import DriverFactory, ExecutorHost, build_executor_host
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
from apps.settings import StartupSettings
from runtime.persistence.database import create_engine


_FRAMEWORK_ERROR_CODES: dict[int, str] = {404: NOT_FOUND, 405: METHOD_NOT_ALLOWED}


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
    """启动时校验配置、确保引擎与会话工厂就绪,并按入口传入的工厂装配执行器。

    配置缺失(数据库 URL、JWT 密钥、主密钥)在这里直接失败,服务不会带着
    公开默认密钥开始接收请求。调用方已注入引擎时视为自己负责配置,不再读环境。
    """
    engine: AsyncEngine | None = getattr(app.state, "engine", None)
    owns_engine = engine is None
    if engine is None:
        settings: StartupSettings = app.state.settings or StartupSettings.from_env()
        app.state.settings = settings
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    host: ExecutorHost | None = None
    try:
        if getattr(app.state, "executor", None) is None:
            host = build_executor_host(engine, app.state.driver_factory)
            await host.start()
            app.state.executor = host.executor
            app.state.executor_driver = host.driver
        yield
    finally:
        if host is not None:
            await host.aclose()
            app.state.executor = None
            app.state.executor_driver = None
        if owns_engine:
            await engine.dispose()


def create_app(
    *,
    engine: AsyncEngine | None = None,
    settings: StartupSettings | None = None,
    driver_factory: DriverFactory | None = None,
    extra_routers: Sequence[APIRouter] = (),
) -> FastAPI:
    """装配 API 应用。

    装配内容全部由调用方显式给出,不读环境变量决定:``driver_factory`` 省略时
    用生产默认 driver(按 Workspace 的模型配置解析 Backend),演示入口显式传
    入脚本 driver 并追加种子路由。测试注入既有引擎与自己的 settings。
    """
    app = FastAPI(title="Agent Runtime API", lifespan=_lifespan)
    app.state.settings = settings
    app.state.driver_factory = driver_factory
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
    app.include_router(workspaces_router)
    app.include_router(memberships_router)
    app.include_router(configuration_router)
    app.include_router(sessions_router)
    app.include_router(runs_router)
    app.include_router(requests_router)
    app.include_router(events_router)
    app.include_router(memories_router)
    for router in extra_routers:
        app.include_router(router)
    return app


# uvicorn 入口(uvicorn apps.api.main:app):引擎与配置由 lifespan 创建。
# 执行器常驻,driver 按各 Workspace 的模型配置解析:没配模型的 Workspace
# 提交消息会得到明确的 409,而不是一个永远停在 QUEUED 的 Run。
app = create_app()
