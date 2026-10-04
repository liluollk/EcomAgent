"""登录与 Workspace 列表路由(计划 04 Task 1;架构文档 §2)。

POST /api/auth/login:本地账号登录,校验密码后签发短期 JWT;
GET /api/workspaces:返回当前 actor 的活动 Workspace 列表,验证认证通路;
真实 Workspace 配置 API 由 Task 2 提供。
"""

from datetime import timedelta
from typing import Final

from fastapi import APIRouter
from sqlalchemy import select

from apps.api.dependencies import ActorDep, SessionDep
from apps.api.errors import INVALID_CREDENTIALS, PERMISSION_DENIED, ApiError
from apps.api.schemas.auth import (
    LoginRequest,
    TokenResponse,
    WorkspaceListResponse,
    WorkspaceSummary,
)
from runtime.access.passwords import verify_password
from runtime.access.tokens import issue_token
from runtime.persistence.base import AccountStatus
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership


router = APIRouter()

LOGIN_TOKEN_TTL: Final = timedelta(minutes=30)
"""登录 JWT 有效期;与 runtime.access.tokens.DEFAULT_TTL 保持一致。"""


@router.post("/api/auth/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: SessionDep) -> TokenResponse:
    """校验登录名与密码;统一返回 INVALID_CREDENTIALS,不泄漏账号存在性。"""
    user = (
        await session.scalars(select(User).where(User.login == payload.login))
    ).one_or_none()
    if (
        user is None
        or user.status != AccountStatus.ACTIVE
        or not verify_password(user.password_hash, payload.password)
    ):
        raise ApiError(status_code=401, code=INVALID_CREDENTIALS, message="登录名或密码不正确")
    token = issue_token(user.id, expires_in=LOGIN_TOKEN_TTL)
    return TokenResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=int(LOGIN_TOKEN_TTL.total_seconds()),
    )


@router.get("/api/workspaces", response_model=WorkspaceListResponse)
async def list_workspaces(
    actor: ActorDep,
    session: SessionDep,
) -> WorkspaceListResponse:
    """返回 actor 当前在 PostgreSQL 中的活动 Workspace 成员身份。

    列表只来自当前库态:成员身份被移除或禁用后,旧 JWT 不再返回任何
    Workspace;当前已无可用成员身份时拒绝访问。
    """
    workspaces = (
        await session.scalars(
            select(Workspace)
            .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
            .where(
                WorkspaceMembership.user_id == actor.actor_id,
                WorkspaceMembership.status == AccountStatus.ACTIVE,
                Workspace.status == AccountStatus.ACTIVE,
            )
            .order_by(Workspace.created_at, Workspace.id)
        )
    ).all()
    if not workspaces:
        raise ApiError(
            status_code=403, code=PERMISSION_DENIED, message="当前账号没有可访问的工作区"
        )
    return WorkspaceListResponse(
        workspaces=[
            WorkspaceSummary(id=workspace.id, name=workspace.name) for workspace in workspaces
        ]
    )
