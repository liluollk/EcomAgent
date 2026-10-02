"""只追加审计服务(计划 01 Task 7;架构文档《身份与 Workspace 权限设计》§10)。

审计语义:
- 事件只追加;``update`` 是显式拒绝修改的占位守卫,不存在 delete 或任何其他
  修改途径,普通业务 API 无法改写历史(架构文档 §10:审计记录只追加);
- 服务绑定写入者身份(workspace + actor,来自调用方的 ActorContext);
  读取方法显式携带 ``workspace_id``,查询绝不返回该参数之外 Workspace 的行;
- 与调用方共享同一 AsyncSession 事务:只 ``flush`` 不 ``commit``,
  事务边界归 Application Service 所有;
- payload 在入库前经审计域脱敏(复用 runtime.security.redaction),
  数据库 JSONB 只存脱敏后内容,原始 Secret 不落库。
"""

import uuid
from collections.abc import Mapping
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import (
    OUTCOME_SUCCESS,
    TARGET_TYPE_ACTION,
    AppendAuditEvent,
    AuditEventView,
)
from runtime.audit.redaction import redact_audit_payload
from runtime.persistence.models.events import AuditEvent


class AuditMutationRejected(RuntimeError):
    """审计事件只追加;任何修改或删除尝试都被显式拒绝。"""


class AuditService:
    """audit_events 的只追加写入与 Workspace 内读取;不提供 Update/Delete。"""

    def __init__(
        self, session: AsyncSession, *, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id

    async def append(
        self,
        action: str,
        *,
        payload: dict[str, Any] | None = None,
        target_type: str = TARGET_TYPE_ACTION,
        target_id: str | None = None,
        outcome: str = OUTCOME_SUCCESS,
        reason_code: str | None = None,
        request_id: str | None = None,
        run_id: uuid.UUID | None = None,
        tool_call_id: uuid.UUID | None = None,
    ) -> AuditEventView:
        """追加一条审计事件并返回其只读视图;只 flush,事务由调用方提交或回滚。

        未显式给目标时,目标即动作本身(target_type="ACTION"、target_id=action);
        失败动作必须显式传 ``outcome=OUTCOME_FAILURE`` 与 ``reason_code``。
        """
        event_input = AppendAuditEvent(
            action=action,
            payload=payload if payload is not None else {},
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            reason_code=reason_code,
            request_id=request_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        row = AuditEvent(
            workspace_id=self._workspace_id,
            actor_id=self._actor_id,
            action=event_input.action,
            target_type=event_input.target_type,
            target_id=event_input.target_id,
            outcome=event_input.outcome,
            reason_code=event_input.reason_code,
            request_id=event_input.request_id,
            run_id=event_input.run_id,
            tool_call_id=event_input.tool_call_id,
            redacted_payload=redact_audit_payload(event_input.payload),
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)  # 载入 server default created_at,保证视图完整。
        return AuditEventView.model_validate(row, from_attributes=True)

    async def update(self, event_id: uuid.UUID, patch: Mapping[str, Any]) -> NoReturn:
        """占位守卫:审计事件只追加,任何修改尝试都抛出 AuditMutationRejected。"""
        raise AuditMutationRejected(
            f"audit event {event_id} is append-only; rejected patch keys: {sorted(patch)}"
        )

    async def list_for_workspace(self, *, workspace_id: uuid.UUID) -> list[AuditEventView]:
        """按 created_at 倒序列出 Workspace 的审计事件(索引 ix_audit_events_workspace_created_at)。"""
        rows = (
            await self._session.scalars(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == workspace_id)
                .order_by(AuditEvent.created_at.desc())
            )
        ).all()
        return [AuditEventView.model_validate(row, from_attributes=True) for row in rows]

    async def list_for_run(
        self, *, workspace_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[AuditEventView]:
        """列出与 Run 关联的审计事件(索引 ix_audit_events_run_created_at)。"""
        rows = (
            await self._session.scalars(
                select(AuditEvent)
                .where(AuditEvent.workspace_id == workspace_id, AuditEvent.run_id == run_id)
                .order_by(AuditEvent.created_at.asc())
            )
        ).all()
        return [AuditEventView.model_validate(row, from_attributes=True) for row in rows]

    async def get(
        self, *, workspace_id: uuid.UUID, event_id: uuid.UUID
    ) -> AuditEventView | None:
        """按 id 读取单条审计事件;workspace 不匹配返回 None,绝不跨 Workspace。"""
        row = (
            await self._session.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == workspace_id, AuditEvent.id == event_id
                )
            )
        ).first()
        if row is None:
            return None
        return AuditEventView.model_validate(row, from_attributes=True)
