"""从持久化状态重建 Run 的"已回答字段"视图。

InputRequest 回答时把值合并进 ToolCall 的 ``normalized_arguments``,InputRequest
行本身不存值(审计也不落值)。因此进程重启后,门面的内存视图为空,推进前必须
按 ANSWERED 的 InputRequest 回到 ToolCall 上把值取回来。

这是执行侧通用逻辑,不属于任何一个具体 driver,因此独立成本模块:
演示 driver 与真实模型 driver 共用同一实现。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import InputRequestStatus
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.interaction import InputRequest


async def rebuild_answered_view(
    session: AsyncSession, workspace_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, str]:
    """按 ``field_paths`` 白名单从 ToolCall 挂起参数取回已回答值。"""
    requests = (
        await session.scalars(
            select(InputRequest).where(
                InputRequest.workspace_id == workspace_id,
                InputRequest.run_id == run_id,
                InputRequest.status == InputRequestStatus.ANSWERED,
            )
        )
    ).all()
    answered: dict[str, str] = {}
    for request in requests:
        if request.tool_call_id is None:
            continue
        tool_call = (
            await session.scalars(
                select(ToolCall).where(
                    ToolCall.workspace_id == workspace_id,
                    ToolCall.id == request.tool_call_id,
                )
            )
        ).one_or_none()
        if tool_call is None:
            continue
        merged = (
            tool_call.normalized_arguments
            if tool_call.normalized_arguments is not None
            else tool_call.raw_arguments
        )
        if not merged:
            continue
        for field in request.field_paths:
            value = dict(merged).get(field)
            if isinstance(value, str):
                answered[field] = value
    return answered


__all__ = ["rebuild_answered_view"]
