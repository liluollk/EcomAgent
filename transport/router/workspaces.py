"""
工作空间管理路由 — 创建/查询工作空间（多品牌隔离入口）。

依赖 transport.state 中的共享状态与 _get_workspace_or_create 惰性注册表。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from starlette.responses import JSONResponse

from session.workspace import Workspace
from transport.state import (
    workspaces,
    _get_workspace_or_create,
)

router = APIRouter()


@router.get("/workspaces")
async def list_workspaces() -> JSONResponse:
    """列出所有工作空间。"""
    # 确保 default 工作空间存在
    _get_workspace_or_create("default")
    return JSONResponse(
        content=[
            {
                "workspace_id": ws.workspace_id,
                "name": ws.name,
                "brand": ws.metadata.get("brand", ws.name),
                "rules": ws.rules,
                "created_at": ws.created_at.isoformat(),
            }
            for ws in workspaces.values()
        ]
    )


@router.post("/workspaces")
async def create_workspace(payload: dict) -> JSONResponse:
    """创建工作空间（多品牌场景：每个品牌一个独立 workspace）。"""
    workspace_id = str(payload.get("workspace_id") or payload.get("name") or "").strip()
    if not workspace_id:
        return JSONResponse(content={"error": "workspace_id 或 name 必填"}, status_code=400)
    if workspace_id in workspaces:
        return JSONResponse(content={"error": f"工作空间已存在: {workspace_id}"}, status_code=400)
    name = str(payload.get("name") or workspace_id)
    brand = str(payload.get("brand") or name)
    rules = payload.get("rules") if isinstance(payload.get("rules"), list) else [{"type": "price_above_cost"}]
    ws = Workspace(
        workspace_id=workspace_id,
        name=name,
        metadata={"brand": brand},
        rules=rules,
    )
    workspaces[workspace_id] = ws
    return JSONResponse(
        content={
            "workspace_id": ws.workspace_id,
            "name": ws.name,
            "brand": brand,
            "rules": ws.rules,
            "created_at": ws.created_at.isoformat(),
        }
    )