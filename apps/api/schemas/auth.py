"""认证与 Workspace 列表的请求/响应模型(计划 04 Task 1)。"""

from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """本地账号登录请求。"""

    login: str = Field(min_length=1)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """登录成功后的短期 JWT。"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int


class WorkspaceSummary(BaseModel):
    """Workspace 列表项。"""

    id: UUID
    name: str


class WorkspaceListResponse(BaseModel):
    """当前 actor 的 Workspace 列表。"""

    workspaces: list[WorkspaceSummary]
