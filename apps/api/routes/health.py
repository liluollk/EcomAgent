"""进程存活与就绪探针路由。

两个端点职责不同,不能合并:

- ``GET /api/health``:liveness,只回答"进程还在不在"。不碰数据库、不碰执行器,
  因此不会被下游抖动带崩,容器据此决定是否重启;
- ``GET /api/ready``:readiness,回答"现在能不能真正推进任务"。检查数据库可达
  与进程内执行器是否仍在运行。

分开的理由:执行器后台循环一旦退出,进程仍然存活、仍然能接收请求,但所有 Run
会永远停在 QUEUED。这种"活着却不干活"的状态只有 readiness 能表达;把它塞进
liveness 会让编排层在数据库短暂抖动时反复重启容器。
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)

router = APIRouter()


class HealthView(BaseModel):
    """GET /api/health 响应。"""

    status: str


class ReadyView(BaseModel):
    """GET /api/ready 响应;``checks`` 逐项给出 ``ok`` 或失败原因。"""

    status: str
    checks: dict[str, str]


@router.get("/api/health", response_model=HealthView)
async def health() -> HealthView:
    """进程存活探针;无认证、无数据库访问。"""
    return HealthView(status="ok")


@router.get("/api/ready", response_model=ReadyView)
async def ready(request: Request) -> ReadyView | JSONResponse:
    """就绪探针:数据库可达 + 执行器循环仍在运行。"""
    checks: dict[str, str] = {}

    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:
        checks["database"] = "not_configured"
    else:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:  # noqa: BLE001 — 探测失败本身就是要报告的结果
            logger.warning("readiness: database probe failed: %s", error)
            checks["database"] = "unreachable"
        else:
            checks["database"] = "ok"

    executor = getattr(request.app.state, "executor", None)
    if executor is None:
        checks["executor"] = "not_started"
    elif not executor.healthy:
        # 循环已退出:进程还活着,但任何 Run 都不会再被推进。
        logger.error("readiness: executor loop is not running")
        checks["executor"] = "stopped"
    elif executor.consecutive_failures:
        checks["executor"] = f"degraded: {executor.consecutive_failures} consecutive failures"
    else:
        checks["executor"] = "ok"

    failed = [name for name, value in checks.items() if value != "ok"]
    body = ReadyView(status="not_ready" if failed else "ready", checks=checks)
    if failed:
        return JSONResponse(status_code=503, content=body.model_dump())
    return body
