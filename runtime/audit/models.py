"""审计读取视图与追加输入模型(计划 01 Task 7;audit_events 实际列)。

- ``AuditEventView``:audit_events 行的只读视图,列一一对应,不可变;
- ``AppendAuditEvent``:append 的 frozen 输入模型,字段宽度与列宽一致;
  未显式给目标时,目标即动作本身(target_type="ACTION"、target_id=action)。

失败动作必须显式传 ``outcome=OUTCOME_FAILURE`` 与 ``reason_code``,默认
``OUTCOME_SUCCESS`` 仅适用于"动作已发生、事后补记"的审计事件。
"""

import uuid
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator


TARGET_TYPE_ACTION: Final = "ACTION"
OUTCOME_SUCCESS: Final = "SUCCESS"
OUTCOME_FAILURE: Final = "FAILURE"


class AppendAuditEvent(BaseModel):
    """append 输入;``payload`` 为原始值,脱敏由 AuditService 在入库前完成。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(max_length=128)
    target_type: str = Field(default=TARGET_TYPE_ACTION, max_length=64)
    target_id: str | None = Field(default=None, max_length=128)
    outcome: str = Field(default=OUTCOME_SUCCESS, max_length=32)
    reason_code: str | None = Field(default=None, max_length=64)
    request_id: str | None = Field(default=None, max_length=128)
    run_id: uuid.UUID | None = None
    tool_call_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _default_target_id_to_action(cls, data: Any) -> Any:
        """target_id 缺省回退为 action:目标即动作本身。"""
        if isinstance(data, dict) and data.get("target_id") is None:
            data = {**data, "target_id": data.get("action")}
        return data


class AuditEventView(BaseModel):
    """audit_events 行的只读视图;与表列一一对应。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: uuid.UUID
    created_at: datetime
    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    action: str
    target_type: str
    target_id: str
    outcome: str
    reason_code: str | None
    request_id: str | None
    run_id: uuid.UUID | None
    tool_call_id: uuid.UUID | None
    redacted_payload: dict[str, Any]
