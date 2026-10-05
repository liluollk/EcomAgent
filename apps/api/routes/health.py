"""进程存活探针路由(阶段 4 Gate;docker compose healthcheck 用)。

只报告进程存活,不做数据库探测——数据库可达性由 Worker/Service 层语义
覆盖;compose 的 api 服务以本端点作为健康判定。
"""

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter()


class HealthView(BaseModel):
    """GET /api/health 响应。"""

    status: str


@router.get("/api/health", response_model=HealthView)
async def health() -> HealthView:
    """进程存活探针;无认证、无数据库访问。"""
    return HealthView(status="ok")
