"""Session 命令与快照 Schema(计划 04 Task 3;架构文档 §5)。

创建 Session 绑定当前 actor;提交用户消息返回创建的 Run;快照包含
messages 与 runs(私有 Session 语义:非创建者一律 404,Schema 不泄漏)。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateSessionRequest(BaseModel):
    """POST /api/sessions 请求体。"""

    workspace_id: UUID
    agent_id: UUID
    title: str | None = Field(default=None, max_length=255)


class SessionView(BaseModel):
    """Session 创建结果;``created_by_actor_id`` 绑定当前 actor。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    agent_id: UUID
    title: str | None
    status: str
    created_by_actor_id: UUID


class AppendMessageRequest(BaseModel):
    """POST /api/sessions/{id}/messages 请求体。"""

    content: str = Field(min_length=1, max_length=100_000)
    model_config_version_id: UUID | None = None


class RunDispatchView(BaseModel):
    """提交用户消息的结果:消息与随之创建的 QUEUED Run。"""

    message_id: UUID
    run_id: UUID
    status: str


class MessageView(BaseModel):
    """消息快照项(Session/Run 快照共用)。"""

    id: UUID
    run_id: UUID | None
    role: str
    status: str
    content: str
    sequence_no: int


class RunSummaryView(BaseModel):
    """Session 快照内的 Run 摘要。"""

    id: UUID
    status: str
    created_at: datetime


class SessionSnapshotView(BaseModel):
    """GET /api/sessions/{id}:私有 Session 的完整快照。"""

    id: UUID
    workspace_id: UUID
    agent_id: UUID
    title: str | None
    status: str
    created_by_actor_id: UUID
    active_run_id: UUID | None
    messages: list[MessageView]
    runs: list[RunSummaryView]
