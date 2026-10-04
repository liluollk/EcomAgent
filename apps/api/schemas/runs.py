"""Run 快照与 Request 处理结果 Schema(计划 04 Task 3;架构文档 §5、§8、§9)。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from apps.api.schemas.sessions import MessageView


class ToolCallView(BaseModel):
    """ToolCall 快照项。"""

    id: UUID
    ordinal: int
    status: str
    raw_arguments: dict[str, Any]
    failure_code: str | None


class RunSnapshotView(BaseModel):
    """GET /api/runs/{id}:Run 快照,``events_cursor`` 为最后已持久事件序号。"""

    id: UUID
    session_id: UUID
    workspace_id: UUID
    status: str
    state_version: int
    events_cursor: int
    messages: list[MessageView]
    tool_calls: list[ToolCallView]


class RunStatusView(BaseModel):
    """POST /api/runs/{id}/cancel 的结果。"""

    id: UUID
    status: str


class RequestDecisionView(BaseModel):
    """Input/Confirmation Request 单次处理结果。"""

    request_id: UUID
    kind: str
    status: str
    run_id: UUID
    run_status: str
