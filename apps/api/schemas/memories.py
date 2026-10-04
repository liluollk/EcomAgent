"""Memory Workbench API 请求/响应模型(计划 04 Task 7)。

响应视图只暴露安全字段:不含 ``deleted_at`` / ``deleted_by_actor_id`` /
``sync_status`` 等内部与敏感元数据。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CorrectMemoryRequest(BaseModel):
    """POST ``/memories/{memory_id}/corrections`` 请求体。"""

    model_config = ConfigDict(frozen=True)

    content: str = Field(min_length=1, max_length=8192)


class MemoryView(BaseModel):
    """单条记忆的安全视图。"""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    subject_key: str
    content: str
    revision: int
    lifecycle_status: str
    supersedes_id: UUID | None
    valid_from: datetime
    expires_at: datetime | None
    created_at: datetime


class MemoryListResponse(BaseModel):
    """GET ``/memories`` 响应;只包含当前 actor 自己的记忆。"""

    model_config = ConfigDict(frozen=True)

    memories: list[MemoryView]


__all__ = ["CorrectMemoryRequest", "MemoryListResponse", "MemoryView"]
