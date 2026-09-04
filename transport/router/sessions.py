"""
会话管理路由 — 创建/查询/删除会话。

依赖 transport.state 中的共享状态（sessions / workspaces / _persist / _restore_from_disk 等）。
"""

from __future__ import annotations

import uuid
import os
from typing import Optional

from fastapi import APIRouter
from starlette.responses import JSONResponse

from session.session import Session, PermissionMode
from transport.schemas import CreateSessionRequest, SetSessionModeRequest
from transport.state import (
    sessions,
    _get_workspace_or_create,
    _persist,
    _restore_from_disk,
    _backend_provider_name,
    _backend_model_name,
    validate_mode,
    apply_mode_change,
    broadcast_mode,
)

router = APIRouter()


@router.post("/sessions")
async def create_session(request: Optional[CreateSessionRequest] = None) -> JSONResponse:
    """创建新会话，绑定用户身份（user_id / role / 可选初始权限模式 / 所属工作空间）。"""
    session_id = str(uuid.uuid4())[:8]

    role = request.role if request is not None else "operator"
    user_id = (
        request.user_id if request is not None and request.user_id else f"user_{session_id}"
    )
    mode = request.permission_mode if request is not None else "ASK"
    workspace_id = request.workspace_id if request is not None else "default"
    workspace = _get_workspace_or_create(workspace_id or "default")

    session = Session(
        session_id=session_id,
        workspace=workspace,
        permission_mode=PermissionMode[mode],
        active_sources=["taobao", "jd", "douyin"],
        user={"user_id": user_id, "role": role},
        model_state={
            "provider": _backend_provider_name(),
            "model": _backend_model_name(),
        },
    )
    sessions[session_id] = session
    _persist(session)
    return JSONResponse(
        content={
            "session_id": session_id,
            "workspace_id": workspace.workspace_id,
            "user": session.user,
            "model_state": session.model_state,
            "permission_mode": session.permission_mode.name,
            "status": session.status.name,
        }
    )


@router.get("/sessions")
async def list_sessions() -> JSONResponse:
    """列出所有会话（含从磁盘恢复的历史会话）。"""
    _restore_from_disk()
    return JSONResponse(
        content=[
            {
                "session_id": s.session_id,
                "workspace_id": s.workspace.workspace_id,
                "workspace_name": s.workspace.name,
                "status": s.status.name,
                "user": s.user,
                "message_count": len(s.messages),
            }
            for s in sessions.values()
        ]
    )


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str) -> JSONResponse:
    """返回会话消息历史（含工具调用消息结构），供前端恢复展示。"""
    _restore_from_disk()
    session = sessions.get(session_id)
    if session is None:
        return JSONResponse(content={"error": "会话不存在"}, status_code=404)
    return JSONResponse(content=session.messages)


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    """删除指定会话（内存记录与磁盘文件一并删除）。"""
    if session_id not in sessions:
        return JSONResponse(content={"error": "会话不存在"}, status_code=404)
    del sessions[session_id]
    filepath = os.path.join(
        os.environ.get("AGENT_STORAGE_DIR")
        or os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "data", "sessions")
        ),
        f"{session_id}.jsonl",
    )
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except OSError:
            pass
    return JSONResponse(content={"deleted": session_id})


@router.get("/sessions/{session_id}/mode")
async def get_session_mode(session_id: str) -> JSONResponse:
    """返回会话当前权限模式。"""
    session = sessions.get(session_id)
    if session is None:
        return JSONResponse(content={"error": "会话不存在"}, status_code=404)
    return JSONResponse(content={"session_id": session_id, "mode": session.permission_mode.name})


@router.put("/sessions/{session_id}/mode")
async def set_session_mode(session_id: str, request: SetSessionModeRequest) -> JSONResponse:
    """切换会话权限模式：更新 + 持久化 + 向该会话活跃 WS 广播 mode_change。"""
    session = sessions.get(session_id)
    if session is None:
        return JSONResponse(content={"error": "会话不存在"}, status_code=404)
    try:
        mode = apply_mode_change(session, request.mode)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)
    await broadcast_mode(session_id, mode)
    return JSONResponse(content={"session_id": session_id, "mode": mode})