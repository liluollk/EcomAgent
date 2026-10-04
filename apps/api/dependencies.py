"""FastAPI 依赖:请求标识、数据库会话与认证身份(计划 04 Task 1;架构文档 §2、§6)。

身份链路:只从 ``Authorization: Bearer`` JWT 解析 actor_id,再从 PostgreSQL
当前状态校验账号与成员身份;任何请求头(如 ``X-Actor-Id``)都不能伪造身份。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.errors import ACTOR_UNAVAILABLE, TOKEN_INVALID, UNAUTHENTICATED, ApiError
from runtime.access.actor import ActorContext, ActorContextBuilder, ActorContextError
from runtime.access.tokens import TokenRejected, verify_token
from runtime.persistence.base import AccountStatus
from runtime.persistence.models.identity import User


REQUEST_ID_HEADER: Final = "X-Request-Id"

_bearer_scheme = HTTPBearer(auto_error=False)


def get_request_id(request: Request) -> str:
    """返回当前请求的 request_id;缺失时回退为请求头透传或随机生成。

    ``RequestContextMiddleware`` 会把它写入 ``scope.state``,同一请求内的
    依赖、路由与异常处理器读到的值一致。
    """
    existing = getattr(request.state, "request_id", None)
    if isinstance(existing, str) and existing:
        return existing
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    request.state.request_id = request_id
    return request_id


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """从 ``app.state`` 上的会话工厂创建请求级 Session;结束统一关闭。

    事务边界归应用服务:本依赖不提交,关闭时丢弃未提交变更。
    """
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
"""请求级数据库会话;同一请求内共享同一 Session 实例。"""

CredentialsDep = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]
"""Authorization: Bearer 凭证;缺失或非 Bearer 时为 None。"""


@dataclass(frozen=True)
class AuthenticatedActor:
    """JWT 校验通过且账号在库中仍有效的调用方身份。"""

    actor_id: UUID
    token_id: str
    request_id: str


async def require_authenticated(
    request: Request,
    credentials: CredentialsDep,
    session: SessionDep,
) -> AuthenticatedActor:
    """校验 Bearer JWT 并确认当前账号有效;失败一律 401,错误码区分原因。"""
    request_id = get_request_id(request)
    if credentials is None:
        raise ApiError(status_code=401, code=UNAUTHENTICATED, message="需要 Bearer 访问令牌")
    try:
        token = verify_token(credentials.credentials)
    except TokenRejected as error:
        raise ApiError(status_code=401, code=TOKEN_INVALID, message="访问令牌无效或已过期") from error

    user = (
        await session.scalars(select(User).where(User.id == token.actor_id))
    ).one_or_none()
    if user is None or user.status != AccountStatus.ACTIVE:
        raise ApiError(status_code=401, code=ACTOR_UNAVAILABLE, message="账号不存在或已禁用")
    return AuthenticatedActor(
        actor_id=token.actor_id, token_id=token.token_id, request_id=request_id
    )


ActorDep = Annotated[AuthenticatedActor, Depends(require_authenticated)]
"""经 JWT 与库态双重校验的调用方身份。"""


async def get_actor_context(
    workspace_id: UUID,
    actor: ActorDep,
    session: SessionDep,
) -> ActorContext:
    """从 PostgreSQL 当前库态构建工作区级 ActorContext;失败一律 401。

    供 ``/api/workspaces/{workspace_id}/...`` 等工作区级路由使用(Task 2 起),
    ``workspace_id`` 取自路由路径参数;成员身份缺失或禁用时拒绝构建。
    """
    try:
        return await ActorContextBuilder(session).build(
            actor_id=actor.actor_id,
            workspace_id=workspace_id,
            request_id=actor.request_id,
        )
    except ActorContextError as error:
        raise ApiError(status_code=401, code=ACTOR_UNAVAILABLE, message="成员身份不可用") from error

