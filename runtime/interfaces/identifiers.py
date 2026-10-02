"""强类型标识符与通用资源范围。

标识符采用 NewType(UUID) 风格:类型层面由 mypy 区分误用,运行时退化为
UUID,可被 Pydantic v2 直接校验。最小集合取自架构文档 §10
ToolExecutionContext 中列出的标识字段。
"""

from typing import Literal, NewType
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


WorkspaceId = NewType("WorkspaceId", UUID)
SessionId = NewType("SessionId", UUID)
RunId = NewType("RunId", UUID)
ToolCallId = NewType("ToolCallId", UUID)
OperationId = NewType("OperationId", UUID)
ActorId = NewType("ActorId", UUID)


class ResourceScope(BaseModel):
    """通用资源范围:全部资源或显式 ID 集合(计划 01 Task 2)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["ALL", "SELECTED"]
    resource_ids: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_selected_scope(self) -> "ResourceScope":
        if self.mode == "SELECTED" and not self.resource_ids:
            raise ValueError("SELECTED scope requires resource ids")
        return self
