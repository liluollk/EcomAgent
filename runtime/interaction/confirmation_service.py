"""ConfirmationService:写操作确认请求的创建、裁决与 verify(计划 02 Task 7;架构文档《Runtime执行状态与恢复设计》§4.4、§6)。

权威语义(计划 Step 3):
- 创建时以规范化 JSON 计算确认指纹,绑定 Actor、Workspace、ToolCall、
  operation_id、规范化参数摘要、资源版本、Tool Schema hash、Policy 版本与
  Execution Mode 版本(见 :mod:`runtime.interaction.fingerprints`);
- accept/reject/cancel 都是单次消费的条件更新
  ``WHERE status='PENDING' AND expires_at > now()``;命中零行时:仍 PENDING
  即已过期,就地标 EXPIRED 并拒绝(``REQUEST_EXPIRED``);EXPIRED 同样拒绝;
  其余拒绝(``REQUEST_NOT_PENDING``);
- verify 用当前资源版本(与可选的当前 Tool Schema hash)重算指纹并与存储
  指纹比对——确认后资源版本再变则指纹不匹配(防 TOCTOU);只有
  ACCEPTED 且未过期的确认才可能有效;
- 每次创建、接受、拒绝、取消、过期都经 :class:`AuditService` 写只追加审计;
  payload 只含指纹与资源键等结构性元数据,绝不含参数值。

事务边界:与调用方共享同一 AsyncSession,只 flush 不 commit。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import OUTCOME_FAILURE
from runtime.audit.service import AuditService
from runtime.interaction.fingerprints import (
    ConfirmationBinding,
    confirmation_fingerprint,
    fingerprint_from_parts,
    normalized_arguments_digest,
)
from runtime.persistence.base import ConfirmationRequestStatus
from runtime.persistence.models.interaction import ConfirmationRequest
from runtime.persistence.repositories.requests import RequestRepository


# ---- 原因码 ----

REQUEST_NOT_FOUND: Final = "REQUEST_NOT_FOUND"
REQUEST_NOT_PENDING: Final = "REQUEST_NOT_PENDING"
REQUEST_EXPIRED: Final = "REQUEST_EXPIRED"
CONFIRMATION_STALE: Final = "CONFIRMATION_STALE"
CONFIRMATION_NOT_ACCEPTED: Final = "CONFIRMATION_NOT_ACCEPTED"

# ---- 审计动作 ----

CONFIRMATION_CREATED: Final = "CONFIRMATION_CREATED"
CONFIRMATION_ACCEPTED: Final = "CONFIRMATION_ACCEPTED"
CONFIRMATION_REJECTED: Final = "CONFIRMATION_REJECTED"
CONFIRMATION_CANCELLED: Final = "CONFIRMATION_CANCELLED"
CONFIRMATION_EXPIRED: Final = "CONFIRMATION_EXPIRED"
CONFIRMATION_DECISION_REJECTED: Final = "CONFIRMATION_DECISION_REJECTED"


class _Unset:
    """哨兵:区分"调用方未提供当前策略修订"与"当前确实没有绑定策略(None)"。"""


_UNSET: Final = _Unset()

TARGET_TYPE_CONFIRMATION_REQUEST: Final = "confirmation_request"


class ConfirmationRequestError(RuntimeError):
    """Confirmation Request 操作冲突;``reason_code`` 供调用方分类处理。

    异常文本以 ``reason_code`` 开头,便于 ``pytest.raises(match=...)`` 与
    日志检索。
    """

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code if message is None else f"{reason_code}: {message}")


@dataclass(frozen=True)
class VerifyResult:
    """确认校验结果;``valid`` 为 False 时 ``reason_code`` 说明原因。"""

    valid: bool
    reason_code: str | None = None


def _rowcount(result: Any) -> int:
    """条件更新影响的行数(与 RunRepository.compare_and_set_status 同构)。"""
    return cast("CursorResult[Any]", result).rowcount


class ConfirmationService:
    """confirmation_requests 的应用服务;条件更新与审计在同一事务内完成。"""

    def __init__(
        self, session: AsyncSession, *, workspace_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._requests = RequestRepository(session)

    def _audit(self, actor_id: uuid.UUID | None) -> AuditService:
        """绑定"当前操作者"的审计服务;缺省回落到服务构造时的 actor。"""
        return AuditService(
            self._session,
            workspace_id=self._workspace_id,
            actor_id=self._actor_id if actor_id is None else actor_id,
        )

    async def create(
        self,
        *,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        resource_versions: Mapping[str, Any],
        expires_at: datetime,
        operation_id: uuid.UUID | None = None,
        tool_schema_hash: str | None = None,
        tool_schema_version: int | None = None,
        policy_version: int | None = None,
        execution_mode_version: int | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> ConfirmationRequest:
        """创建 PENDING Confirmation Request 并计算确认指纹。

        指纹绑定要素即 :class:`~runtime.interaction.fingerprints.ConfirmationBinding`
        的字段;``normalized_arguments_hash`` 单独入列,供 verify 重算指纹。
        ``actor_id`` 是需要做出确认的 Actor。
        """
        confirming_actor = self._actor_id if actor_id is None else actor_id
        fingerprint = confirmation_fingerprint(
            ConfirmationBinding(
                actor_id=confirming_actor,
                workspace_id=self._workspace_id,
                tool_call_id=tool_call_id,
                operation_id=operation_id,
                normalized_arguments=dict(normalized_arguments),
                resource_versions=dict(resource_versions),
                tool_schema_hash=tool_schema_hash,
                policy_version=policy_version,
                execution_mode_version=execution_mode_version,
            )
        )
        request = await self._requests.append_confirmation_request(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            actor_id=confirming_actor,
            fingerprint=fingerprint,
            normalized_arguments_hash=normalized_arguments_digest(normalized_arguments),
            resource_versions=dict(resource_versions),
            expires_at=expires_at,
            operation_id=operation_id,
            tool_schema_version=tool_schema_version,
            policy_version=policy_version,
            execution_mode_version=execution_mode_version,
        )
        await self._audit(confirming_actor).append(
            CONFIRMATION_CREATED,
            payload={
                "fingerprint": fingerprint,
                "resource_keys": sorted(str(key) for key in resource_versions),
            },
            target_type=TARGET_TYPE_CONFIRMATION_REQUEST,
            target_id=str(request.id),
            request_id=str(request.id),
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        return request

    async def accept(
        self,
        confirmation_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> ConfirmationRequest:
        """接受确认(单次消费条件更新);返回 ACCEPTED 行。"""
        return await self._decide(
            confirmation_id,
            ConfirmationRequestStatus.ACCEPTED,
            actor_id=actor_id,
            now=now,
        )

    async def reject(
        self,
        confirmation_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> ConfirmationRequest:
        """拒绝确认(单次消费条件更新);REJECTED 不执行 Tool。"""
        return await self._decide(
            confirmation_id,
            ConfirmationRequestStatus.REJECTED,
            actor_id=actor_id,
            now=now,
        )

    async def cancel(
        self,
        confirmation_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> ConfirmationRequest:
        """取消确认(单次消费条件更新)。"""
        return await self._decide(
            confirmation_id,
            ConfirmationRequestStatus.CANCELLED,
            actor_id=actor_id,
            now=now,
        )

    async def verify(
        self,
        confirmation_id: uuid.UUID,
        *,
        current_resource_versions: Mapping[str, Any],
        tool_schema_hash: str | None = None,
        policy_version: int | None | _Unset = _UNSET,
        now: datetime | None = None,
    ) -> VerifyResult:
        """校验已接受的确认是否仍然有效(只读,不迁移状态)。

        用当前资源版本(与可选的当前 Tool Schema hash、当前业务策略修订)重算指纹
        并与存储指纹比对:资源版本、参数、Schema、Policy、Execution Mode 任一变化
        都会改变指纹,旧确认即失效(``CONFIRMATION_STALE``)。``tool_schema_hash``
        缺省 None 与 create 缺省 None 一致;create 绑定了 hash 而 verify 未提供当前
        hash 时按失败闭合处理(视为已漂移)。

        ``policy_version`` 是**当前**业务策略修订号:调用方读得到当前规则时必须传入,
        否则"策略更新后仍接受旧确认"无法被发现;未传入(哨兵)时退回存储值,保持
        create/verify 对称的旧行为。传入 ``None`` 表示当前确实没有绑定策略。
        """
        moment = datetime.now(UTC) if now is None else now
        request = await self._requests.get_confirmation_request(
            self._workspace_id, confirmation_id
        )
        if request is None:
            return VerifyResult(valid=False, reason_code=REQUEST_NOT_FOUND)
        if request.status == ConfirmationRequestStatus.EXPIRED:
            return VerifyResult(valid=False, reason_code=REQUEST_EXPIRED)
        if request.status != ConfirmationRequestStatus.ACCEPTED:
            return VerifyResult(valid=False, reason_code=CONFIRMATION_NOT_ACCEPTED)
        if request.expires_at <= moment:
            return VerifyResult(valid=False, reason_code=REQUEST_EXPIRED)
        current_policy_version = (
            request.policy_version if isinstance(policy_version, _Unset) else policy_version
        )
        expected = fingerprint_from_parts(
            actor_id=request.actor_id,
            workspace_id=request.workspace_id,
            tool_call_id=request.tool_call_id,
            operation_id=request.operation_id,
            normalized_arguments_hash=request.normalized_arguments_hash,
            resource_versions=dict(current_resource_versions),
            tool_schema_hash=tool_schema_hash,
            policy_version=current_policy_version,
            execution_mode_version=request.execution_mode_version,
        )
        if expected != request.fingerprint:
            return VerifyResult(valid=False, reason_code=CONFIRMATION_STALE)
        return VerifyResult(valid=True, reason_code=None)

    # ---- 内部 ----

    async def _decide(
        self,
        confirmation_id: uuid.UUID,
        target_status: ConfirmationRequestStatus,
        *,
        actor_id: uuid.UUID | None,
        now: datetime | None,
    ) -> ConfirmationRequest:
        """单次消费裁决:条件更新 PENDING → 目标状态,零行时区分过期。"""
        moment = datetime.now(UTC) if now is None else now
        actor = self._actor_id if actor_id is None else actor_id
        audit = self._audit(actor)
        moved = await self._session.execute(
            sa.update(ConfirmationRequest)
            .where(
                ConfirmationRequest.id == confirmation_id,
                ConfirmationRequest.workspace_id == self._workspace_id,
                ConfirmationRequest.status == ConfirmationRequestStatus.PENDING,
                ConfirmationRequest.expires_at > moment,
            )
            .values(status=target_status, handled_at=moment)
            .execution_options(synchronize_session=False)
        )
        if _rowcount(moved) != 1:
            fresh = await self._requests.get_confirmation_request(
                self._workspace_id, confirmation_id
            )
            if fresh is None:
                await self._write_decision_rejection(
                    audit, confirmation_id, REQUEST_NOT_FOUND
                )
                raise ConfirmationRequestError(
                    REQUEST_NOT_FOUND,
                    f"confirmation request {confirmation_id} not found "
                    f"in workspace {self._workspace_id}",
                )
            if fresh.status == ConfirmationRequestStatus.PENDING:
                # 仍 PENDING 即 expires_at <= now:就地过期并拒绝(计划语义)。
                await self._mark_expired(confirmation_id)
                await audit.append(
                    CONFIRMATION_EXPIRED,
                    target_type=TARGET_TYPE_CONFIRMATION_REQUEST,
                    target_id=str(confirmation_id),
                    request_id=str(confirmation_id),
                    run_id=fresh.run_id,
                    tool_call_id=fresh.tool_call_id,
                )
                await self._write_decision_rejection(
                    audit, confirmation_id, REQUEST_EXPIRED
                )
                raise ConfirmationRequestError(
                    REQUEST_EXPIRED, f"confirmation request {confirmation_id} expired"
                )
            reason = (
                REQUEST_EXPIRED
                if fresh.status == ConfirmationRequestStatus.EXPIRED
                else REQUEST_NOT_PENDING
            )
            await self._write_decision_rejection(audit, confirmation_id, reason)
            raise ConfirmationRequestError(
                reason, f"confirmation request {confirmation_id} is {fresh.status.value}"
            )
        request = await self._requests.get_confirmation_request(
            self._workspace_id, confirmation_id
        )
        if request is None:  # pragma: no cover - 条件更新成功后行必然存在
            raise ConfirmationRequestError(REQUEST_NOT_FOUND, "confirmation vanished")
        action = {
            ConfirmationRequestStatus.ACCEPTED: CONFIRMATION_ACCEPTED,
            ConfirmationRequestStatus.REJECTED: CONFIRMATION_REJECTED,
            ConfirmationRequestStatus.CANCELLED: CONFIRMATION_CANCELLED,
        }[target_status]
        await audit.append(
            action,
            payload={"fingerprint": request.fingerprint},
            target_type=TARGET_TYPE_CONFIRMATION_REQUEST,
            target_id=str(confirmation_id),
            request_id=str(confirmation_id),
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
        )
        return request

    async def _mark_expired(self, confirmation_id: uuid.UUID) -> None:
        """把仍处 PENDING 的确认标 EXPIRED(过期兜底,不带时间条件)。"""
        await self._session.execute(
            sa.update(ConfirmationRequest)
            .where(
                ConfirmationRequest.id == confirmation_id,
                ConfirmationRequest.workspace_id == self._workspace_id,
                ConfirmationRequest.status == ConfirmationRequestStatus.PENDING,
            )
            .values(status=ConfirmationRequestStatus.EXPIRED)
            .execution_options(synchronize_session=False)
        )

    async def _write_decision_rejection(
        self, audit: AuditService, confirmation_id: uuid.UUID, reason_code: str
    ) -> None:
        """写裁决拒绝审计。"""
        await audit.append(
            CONFIRMATION_DECISION_REJECTED,
            outcome=OUTCOME_FAILURE,
            reason_code=reason_code,
            target_type=TARGET_TYPE_CONFIRMATION_REQUEST,
            target_id=str(confirmation_id),
            request_id=str(confirmation_id),
        )
