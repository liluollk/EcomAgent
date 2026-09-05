"""审批中心 — 跨会话权限请求的聚合审批 API。

权限请求仍由 WS 连接内的挂起 Future 承载（对话内权限卡照常可用）；
本路由提供第二确认入口：审批中心页面聚合所有会话的待审批请求，
经 REST 决定后唤醒对应挂起的 turn，并留存跨会话审批历史。
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from transport.state import APPROVAL_REGISTRY, sessions
from session.storage import save_session

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _storage_dir() -> str:
    """运行时读取存储目录（兼容测试的 env 隔离）。"""
    return os.environ.get("AGENT_STORAGE_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "sessions")
    )


@router.get("/pending")
async def list_pending():
    """聚合所有会话当前挂起的权限请求（未决的注册表条目）。"""
    pending = []
    for request_id, item in APPROVAL_REGISTRY.items():
        if item["future"].done():
            continue
        pending.append(
            {
                "request_id": request_id,
                "session_id": item["session_id"],
                "tool_name": item["tool_name"],
                "tool_input": item["tool_input"],
                "reason": item["reason"],
                "timestamp": item["timestamp"],
            }
        )
    pending.sort(key=lambda r: r["timestamp"])
    return {"pending": pending, "total": len(pending)}


@router.post("/{request_id}/decision")
async def decide(request_id: str, payload: dict):
    """审批决定：唤醒挂起的 turn 并写回会话权限记录。"""
    item = APPROVAL_REGISTRY.get(request_id)
    if item is None:
        return JSONResponse(status_code=404, content={"error": "审批请求不存在或已处理"})
    approved = bool(payload.get("approved"))
    future = item["future"]
    if not future.done():
        future.set_result(approved)
    session = sessions.get(item["session_id"])
    if session is not None:
        for req in session.permission_requests:
            if req.get("request_id") == request_id:
                req["approved"] = approved
                break
        save_session(session, _storage_dir())
    return {"request_id": request_id, "approved": approved}


@router.get("/history")
async def history():
    """跨会话审批历史：已决（approved 非空）的权限请求记录，按时间倒序。"""
    records = []
    for sid, session in sessions.items():
        for req in session.permission_requests:
            if req.get("approved") is None:
                continue
            records.append(
                {
                    "session_id": sid,
                    "request_id": req.get("request_id"),
                    "tool_name": req.get("tool_name"),
                    "tool_input": req.get("input"),
                    "user": req.get("user"),
                    "timestamp": req.get("timestamp"),
                    "approved": req.get("approved"),
                }
            )
    records.sort(key=lambda r: r.get("timestamp") or 0, reverse=True)
    return {"history": records, "total": len(records)}
