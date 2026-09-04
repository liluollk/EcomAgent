"""
API 请求/响应模型 — Pydantic BaseModel（内部工程使用，不进简历技术栈）。

FastAPI 自动完成 body 解析、字段校验与 400 错误返回，并在 /docs 暴露。
非法 role 由 Literal 约束直接拒绝（400），替代手写 payload 解析 + 静默回退。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from permission.rbac import ALL_ROLES

# role 枚举值（与 RBAC 角色集一致，类型安全）
RoleValue = Literal["manager", "operator", "customer_service", "finance"]

# 权限模式枚举值（对应 session.session.PermissionMode 三模式）
ModeValue = Literal["READONLY", "ASK", "EXECUTE"]


class CreateSessionRequest(BaseModel):
    """创建会话请求体。"""

    user_id: Optional[str] = Field(default=None, description="用户标识，默认 user_<随机>")
    role: RoleValue = Field(default="operator", description="会话绑定角色")
    permission_mode: ModeValue = Field(default="ASK", description="会话初始权限模式")
    workspace_id: Optional[str] = Field(default="default", description="会话所属工作空间（多品牌隔离）")


class SetSessionModeRequest(BaseModel):
    """设置会话权限模式请求体。"""

    mode: ModeValue = Field(description="目标权限模式")


class CreateSessionResponse(BaseModel):
    """创建会话响应体。"""

    session_id: str
    workspace_id: str
    user: dict
    status: str
    permission_mode: str = "ASK"


__all__ = [
    "CreateSessionRequest",
    "CreateSessionResponse",
    "SetSessionModeRequest",
    "RoleValue",
    "ModeValue",
]