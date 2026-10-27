"""AgentEvent WebSocket 流路由(计划 04 Task 4;架构文档《Runtime执行状态与恢复设计》§事件交付)。

``WS /api/runs/{run_id}/events?cursor=<事件 id|序号>``:

- 握手前拒绝:认证(:func:`~apps.api.dependencies.require_ws_actor`,401)、
  Run 范围校验(非当前 actor 有效成员身份的工作区一律 404)与 cursor 解析
  (格式非法 422、事件不存在 404)全部在 ``accept`` 之前完成,拒绝以统一
  错误体作为 WebSocket 拒绝响应下发;
- accept 后先重放 cursor 之后的全部已提交事件,再按 ``poll_interval`` 轮询
  推送新提交事件(at-least-once,客户端按 id 去重、按 sequence_no 检测缺口);
- cursor 通过查询参数指定(选型:查询参数而非首条客户端消息,与浏览器原生
  WebSocket 及 ``collect(cursor=...)`` 用法对齐);
- 客户端上行消息不参与协议,仅用于感知断开并取消推送循环(断开清理)。
"""

import contextlib
import uuid

import anyio
from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from apps.api.command_errors import translated_commands
from apps.api.dependencies import WsActorDep, WsSessionDep
from apps.api.event_stream import EventStreamService
from runtime.commands.session import SessionCommandService


router = APIRouter()


@router.websocket("/api/runs/{run_id}/events")
async def stream_run_events(
    run_id: uuid.UUID,
    actor: WsActorDep,
    session: WsSessionDep,
    websocket: WebSocket,
    cursor: str | None = None,
) -> None:
    """按 Run 重放并推送已提交事件;拒绝均在握手前下发,断开即取消推送。"""
    with translated_commands():
        run = await SessionCommandService(session, actor_id=actor.actor_id).resolve_run(run_id)
    service = EventStreamService(websocket.app.state.sessionmaker)
    start_sequence = await service.resolve_start_sequence(
        workspace_id=run.workspace_id, run_id=run.id, cursor=cursor
    )
    # 范围与 cursor 校验共用握手会话;先取标量再结束其只读事务(rollback 会
    # 过期 ORM 对象),事件读取全部由服务经独立短事务完成。
    workspace_id = run.workspace_id
    run_id = run.id
    await session.rollback()
    await websocket.accept()
    await _push_until_disconnected(websocket, service, workspace_id, run_id, start_sequence)


async def _push_until_disconnected(
    websocket: WebSocket,
    service: EventStreamService,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    start_sequence: int,
) -> None:
    """推放事件直到客户端断开;上行消息仅用于感知断开,不参与协议。"""
    async with anyio.create_task_group() as task_group:

        async def _drain() -> None:
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                task_group.cancel_scope.cancel()

        async def _pump() -> None:
            try:
                async with contextlib.aclosing(
                    service.stream(
                        workspace_id=workspace_id,
                        run_id=run_id,
                        after_sequence=start_sequence,
                    )
                ) as messages:
                    async for message in messages:
                        await websocket.send_json(message.to_json())
            except (WebSocketDisconnect, RuntimeError):
                # 客户端在两次推送之间断开:send 检测到断开状态抛出,
                # 停止推送并结束连接(断开清理)。
                task_group.cancel_scope.cancel()

        task_group.start_soon(_drain)
        try:
            await _pump()
        finally:
            # 推送结束(Run 终态)时必须收尾接收协程:它阻塞在 receive_text
            # 上等客户端发消息,不结束的话 task group 永远不会退出、连接也关不掉。
            # 先 close 再 cancel:close 让挂起的 receive 立即返回,避免等待一个
            # 对端永远不会发送的消息。
            with contextlib.suppress(RuntimeError):
                await websocket.close()
            task_group.cancel_scope.cancel()
