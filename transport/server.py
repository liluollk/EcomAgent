"""
FastAPI 应用入口 — 生命周期管理、WebSocket 双工通信、前端路由。

职责（单一）：
1. 应用创建与生命周期（子进程拉起/MCP 连接/资源释放）
2. 前端 SPA 路由与静态文件服务
3. WebSocket 端点的双工事件流推送

REST 路由（会话 CRUD / 渠道管理）已拆分到 transport/router/ 下。
"""

from __future__ import annotations

import json as _json
import asyncio
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from events.agent_event import AgentEvent
from transport.state import (
    mcp_pool,
    sessions,
    workspaces,
    session_ws_clients,
    apply_mode_change,
    broadcast_mode,
    _start_channel_api,
    _stop_channel_api,
    _persist,
    _build_agent,
    _build_tools,
)
from transport.router.sessions import router as sessions_router
from transport.router.channels import router as channels_router
from transport.router.providers import router as providers_router
from transport.router.workspaces import router as workspaces_router
from transport.router.skills import router as skills_router
from transport.router.workspace_data import router as workspace_data_router
from transport.router.mcp import router as mcp_router

# ---------------------------------------------------------------------------
# 应用生命周期
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _mcp_lifespan(app: FastAPI):
    """启动时拉起 mock 平台服务并连接 MCP Server，关闭时释放资源。"""
    if os.environ.get("CHANNEL_API_AUTO", "1") != "0":
        _start_channel_api()
    try:
        await mcp_pool.connect()
        print(f"[MCP] 已连接: {len(mcp_pool.get_all_tool_definitions())} 个工具经真实协议发现")
    except Exception as exc:
        print(f"[WARN] MCP 连接失败，回退内存 Source: {exc}")
    yield
    await mcp_pool.close()
    _stop_channel_api()


app = FastAPI(title="电商运营 Agent Runtime", version="0.2.0", lifespan=_mcp_lifespan)

# 注册 REST 路由
app.include_router(sessions_router)
app.include_router(channels_router)
app.include_router(providers_router)
app.include_router(workspaces_router)
app.include_router(skills_router)
app.include_router(workspace_data_router)
app.include_router(mcp_router)

# ---------------------------------------------------------------------------
# 前端静态文件
# ---------------------------------------------------------------------------

# Windows 注册表可能把 .js 映射为 text/plain，浏览器对 ES module 强制 MIME
# 校验会拒绝加载；显式修正为标准类型（跨平台无害）。
import mimetypes as _mimetypes

_mimetypes.add_type("application/javascript", ".js")
_mimetypes.add_type("text/css", ".css")

_FRONTEND_DIST_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
os.makedirs(_STATIC_DIR, exist_ok=True)

if os.path.isdir(_FRONTEND_DIST_DIR) and os.path.isfile(
    os.path.join(_FRONTEND_DIST_DIR, "index.html")
):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_FRONTEND_DIST_DIR, "assets")),
        name="frontend_assets",
    )
else:
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def root():
    """返回聊天 UI 页面。优先使用 React 构建产物，否则 fallback 到旧版静态 HTML。"""
    react_index = os.path.join(_FRONTEND_DIST_DIR, "index.html")
    if os.path.isfile(react_index):
        return FileResponse(react_index)
    static_index = os.path.join(_STATIC_DIR, "index.html")
    if os.path.exists(static_index):
        return FileResponse(static_index)
    return JSONResponse(
        content={
            "message": "index.html 未找到，请先构建前端: cd frontend && npm run build"
        },
        status_code=404,
    )


# ---------------------------------------------------------------------------
# WebSocket 双工通信
# ---------------------------------------------------------------------------


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """WebSocket 端点：接收用户消息并推送 AgentEvent JSON 流。

    接收循环与事件推送并发运行：turn 推送期间仍可接收 abort 中断与
    permission_response 权限决定，实现 ASK 模式下的阻塞式人工确认。
    """
    await websocket.accept()

    if session_id not in sessions:
        await websocket.send_json({"type": "error", "message": "会话不存在"})
        await websocket.close()
        return

    session = sessions[session_id]
    agent = _build_agent(session)
    tools = _build_tools()

    # 登记为当前会话的活跃连接，供模式切换等 REST/WS 操作广播 mode_change。
    session_ws_clients.setdefault(session_id, set()).add(websocket)
    try:
        await _ws_loop(session_id, websocket, session, agent, tools)
    finally:
        clients = session_ws_clients.get(session_id)
        if clients is not None:
            clients.discard(websocket)
            if not clients:
                session_ws_clients.pop(session_id, None)


async def _ws_loop(
    session_id: str,
    websocket: WebSocket,
    session,
    agent,
    tools: list[dict],
) -> None:
    """单会话 WebSocket 主循环：流式推送 AgentEvent 期间并发接收控制类消息。"""
    pending_decisions: dict[str, asyncio.Future] = {}
    inbound: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    async def resolve_permission(request) -> bool:
        """权限解析器：挂起直到前端回传 permission_response。"""
        future = loop.create_future()
        pending_decisions[request.request_id] = future
        try:
            return await future
        finally:
            pending_decisions.pop(request.request_id, None)

    agent.set_permission_resolver(resolve_permission)

    def _deny_all_pending() -> None:
        """中断/断连时拒绝所有挂起的权限请求，解除 Agent 阻塞。"""
        for future in pending_decisions.values():
            if not future.done():
                future.set_result(False)
        pending_decisions.clear()

    agent.set_permission_canceller(_deny_all_pending)

    async def receiver() -> None:
        """并发接收循环：分发 abort / permission_response / message / mode_change。"""
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = _json.loads(raw)
                except _json.JSONDecodeError:
                    continue
                msg_type = data.get("type")

                if msg_type == "abort":
                    agent.abort("user_cancel")
                    await websocket.send_json({"type": "status", "message": "已中断"})

                elif msg_type == "compact":
                    changed = agent.compact_session(session)
                    await websocket.send_json(
                        {
                            "type": "status",
                            "message": "已压缩上下文" if changed else "当前上下文无需压缩",
                        }
                    )

                elif msg_type == "permission_response":
                    future = pending_decisions.get(data.get("request_id"))
                    if future is not None and not future.done():
                        future.set_result(bool(data.get("approved")))

                elif msg_type == "mode_change":
                    mode = data.get("mode")
                    apply_mode_change(session, mode)
                    await broadcast_mode(session_id, mode)

                elif msg_type == "message":
                    inbound.put_nowait(data.get("content", ""))
        except Exception:
            _deny_all_pending()
            inbound.put_nowait(None)  # 唤醒主循环退出

    receiver_task = asyncio.create_task(receiver())
    try:
        while True:
            content = await inbound.get()
            if content is None or not content:
                if content is None:
                    break
                await websocket.send_json(
                    {"type": "error", "message": "消息内容为空"}
                )
                continue

            # 流式推送 AgentEvent（期间 abort / 权限回传由 receiver 并发处理）
            async for event in agent.chat(session, content, tools):
                await websocket.send_json(_event_to_dict(event))
            await websocket.send_json({"type": "turn_complete"})
            _persist(session)

    except WebSocketDisconnect:
        pass
    except Exception:
        _deny_all_pending()
    finally:
        receiver_task.cancel()


# ---------------------------------------------------------------------------
# 事件序列化
# ---------------------------------------------------------------------------


def _event_to_dict(event: AgentEvent) -> dict:
    """将 AgentEvent 转换为可 JSON 序列化的字典。

    使用 dataclasses.asdict 递归转换，确保嵌套 dataclass 也被展开。
    """
    from dataclasses import asdict, is_dataclass

    if is_dataclass(event):
        return asdict(event)
    return {"type": "unknown"}