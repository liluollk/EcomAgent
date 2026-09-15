"""
工作空间管理路由 — 创建/查询工作空间（多品牌隔离入口）。

依赖 transport.state 中的共享状态与 _get_workspace_or_create 惰性注册表。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from starlette.responses import JSONResponse

from session.workspace import Workspace
from session.workspace_store import normalize_rules, save_workspace_record
from transport.state import (
    workspaces,
    _get_workspace_or_create,
    _restore_workspaces_from_disk,
    _workspace_record,
)

router = APIRouter()


@router.get("/workspaces")
async def list_workspaces() -> JSONResponse:
    """列出所有工作空间。"""
    # 确保 default 工作空间存在
    _get_workspace_or_create("default")
    _restore_workspaces_from_disk()
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
    try:
        rules = normalize_rules(payload.get("rules"), default=True)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    ws = Workspace(
        workspace_id=workspace_id,
        name=name,
        metadata={"brand": brand},
        rules=rules,
    )
    workspaces[workspace_id] = ws
    save_workspace_record(_workspace_record(ws))
    return JSONResponse(
        content={
            "workspace_id": ws.workspace_id,
            "name": ws.name,
            "brand": brand,
            "rules": ws.rules,
            "created_at": ws.created_at.isoformat(),
        }
    )


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, payload: dict) -> JSONResponse:
    """更新已知 Workspace 规则，并返回规范化后的配置。"""
    ws = workspaces.get(workspace_id) or _get_workspace_or_create(workspace_id)
    if "rules" not in payload:
        return JSONResponse(content={"error": "仅支持更新 rules，且 rules 必填"}, status_code=400)
    try:
        ws.rules = normalize_rules(payload.get("rules"), default=False)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    save_workspace_record(_workspace_record(ws))
    return JSONResponse(
        content={
            "workspace_id": ws.workspace_id,
            "name": ws.name,
            "brand": ws.metadata.get("brand", ws.name),
            "rules": ws.rules,
            "created_at": ws.created_at.isoformat(),
        }
    )
