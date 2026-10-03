"""ExpireService:过期 Request 的批量清扫(计划 02 Task 7)。

语义:
- 把 ``expires_at <= now`` 且仍处 ``PENDING`` 的 Input/Confirmation Request
  批量标为 ``EXPIRED``(条件批量 UPDATE ... RETURNING,行锁保证并发清扫
  互不重复);
- 每个被过期的请求写一条只追加审计事件;审计归属构造时绑定的
  workspace/actor(由调用方传入系统服务身份);
- 返回本次过期的请求总数;重复调用幂等(已过期的行不再匹配条件)。

事务边界:与调用方共享同一 AsyncSession,只 flush 不 commit。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.service import AuditService
from runtime.persistence.base import ConfirmationRequestStatus, InputRequestStatus
from runtime.persistence.models.interaction import ConfirmationRequest, InputRequest


INPUT_REQUEST_EXPIRED: Final = "INPUT_REQUEST_EXPIRED"
CONFIRMATION_EXPIRED: Final = "CONFIRMATION_EXPIRED"

TARGET_TYPE_INPUT_REQUEST: Final = "input_request"
TARGET_TYPE_CONFIRMATION_REQUEST: Final = "confirmation_request"


class ExpireService:
    """过期 Request 清扫服务;审计归属绑定的 workspace + 系统服务 actor。"""

    def __init__(
        self, session: AsyncSession, *, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._audit = AuditService(
            session, workspace_id=workspace_id, actor_id=actor_id
        )

    async def expire_due(self, now: datetime | None = None) -> int:
        """把到期仍未处理的 PENDING 请求标 EXPIRED 并写审计;返回过期数量。"""
        moment = datetime.now(UTC) if now is None else now
        expired = await self._expire_input_requests(moment)
        expired += await self._expire_confirmation_requests(moment)
        return expired

    async def _expire_input_requests(self, now: datetime) -> int:
        """批量过期 Input Request;对每个过期行写审计。"""
        stmt = (
            sa.update(InputRequest)
            .where(
                InputRequest.workspace_id == self._workspace_id,
                InputRequest.status == InputRequestStatus.PENDING,
                InputRequest.expires_at <= now,
            )
            .values(status=InputRequestStatus.EXPIRED)
            .returning(
                InputRequest.id.label("request_id"),
                InputRequest.run_id,
                InputRequest.tool_call_id,
            )
            .execution_options(synchronize_session=False)
        )
        rows = (await self._session.execute(stmt)).all()
        for row in rows:
            await self._write_expired_audit(
                action=INPUT_REQUEST_EXPIRED,
                target_type=TARGET_TYPE_INPUT_REQUEST,
                request_id=row.request_id,
                run_id=row.run_id,
                tool_call_id=row.tool_call_id,
            )
        return len(rows)

    async def _expire_confirmation_requests(self, now: datetime) -> int:
        """批量过期 Confirmation Request;对每个过期行写审计。"""
        stmt = (
            sa.update(ConfirmationRequest)
            .where(
                ConfirmationRequest.workspace_id == self._workspace_id,
                ConfirmationRequest.status == ConfirmationRequestStatus.PENDING,
                ConfirmationRequest.expires_at <= now,
            )
            .values(status=ConfirmationRequestStatus.EXPIRED)
            .returning(
                ConfirmationRequest.id.label("request_id"),
                ConfirmationRequest.run_id,
                ConfirmationRequest.tool_call_id,
            )
            .execution_options(synchronize_session=False)
        )
        rows = (await self._session.execute(stmt)).all()
        for row in rows:
            await self._write_expired_audit(
                action=CONFIRMATION_EXPIRED,
                target_type=TARGET_TYPE_CONFIRMATION_REQUEST,
                request_id=row.request_id,
                run_id=row.run_id,
                tool_call_id=row.tool_call_id,
            )
        return len(rows)

    async def _write_expired_audit(
        self,
        *,
        action: str,
        target_type: str,
        request_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_call_id: uuid.UUID | None,
    ) -> None:
        """写单条过期审计事件。"""
        await self._audit.append(
            action,
            target_type=target_type,
            target_id=str(request_id),
            request_id=str(request_id),
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
