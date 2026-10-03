"""InputService:durable Input Request 的创建、回答与取消(计划 02 Task 7;架构文档《Runtime执行状态与恢复设计》§4.4、§5)。

权威语义(计划 Step 3):
- 回答只合并 ``field_paths`` 声明的字段,前端不能替换整个 ToolCall 参数;
  声明外字段抛 :class:`InputAnswerRejected`(``UNDECLARED_FIELD``);
- 回答使用条件更新 ``WHERE status='PENDING' AND expires_at > now()`` 实现单次
  消费与 TOCTOU 兜底;命中零行时按行状态区分:仍 PENDING 即已过期,就地标
  EXPIRED 并拒绝(``REQUEST_EXPIRED``);EXPIRED 同样拒绝;其余拒绝
  (``REQUEST_NOT_PENDING``);
- 合并结果写回 ToolCall 的挂起参数(``normalized_arguments``,缺省时以
  ``raw_arguments`` 为基),并随返回值携带——恢复后从 Tool Resolution 重走
  PreToolUse 的完整管线由后续 Loop 任务(Task 9)执行;
- 每次创建、回答、拒绝、取消、过期都经 :class:`AuditService` 写只追加审计;
  payload 只含结构性元数据(field_paths 等),绝不含回答值。

事务边界:与调用方共享同一 AsyncSession,只 flush 不 commit。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.audit.models import OUTCOME_FAILURE
from runtime.audit.service import AuditService
from runtime.persistence.base import InputRequestStatus
from runtime.persistence.models.execution import ToolCall
from runtime.persistence.models.interaction import InputRequest
from runtime.persistence.repositories.requests import RequestRepository


# ---- 拒绝原因码 ----

REQUEST_NOT_FOUND: Final = "REQUEST_NOT_FOUND"
REQUEST_NOT_PENDING: Final = "REQUEST_NOT_PENDING"
REQUEST_EXPIRED: Final = "REQUEST_EXPIRED"
UNDECLARED_FIELD: Final = "UNDECLARED_FIELD"

# ---- 审计动作 ----

INPUT_REQUEST_CREATED: Final = "INPUT_REQUEST_CREATED"
INPUT_ANSWERED: Final = "INPUT_ANSWERED"
INPUT_ANSWER_REJECTED: Final = "INPUT_ANSWER_REJECTED"
INPUT_REQUEST_CANCELLED: Final = "INPUT_REQUEST_CANCELLED"
INPUT_REQUEST_EXPIRED: Final = "INPUT_REQUEST_EXPIRED"

TARGET_TYPE_INPUT_REQUEST: Final = "input_request"


class InputRequestError(RuntimeError):
    """Input Request 操作冲突;``reason_code`` 供调用方分类处理。

    异常文本以 ``reason_code`` 开头,便于 ``pytest.raises(match=...)`` 与
    日志检索。
    """

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code if message is None else f"{reason_code}: {message}")


class InputAnswerRejected(InputRequestError):
    """回答被拒绝(计划原文异常);``reason_code`` 说明拒绝原因。"""


@dataclass(frozen=True)
class InputAnswer:
    """成功回答的结果:携带 Task 9 重走 PreToolUse 所需的最小信息。

    ``merged_arguments`` 是合并声明字段后的完整挂起参数;恢复管线从
    Tool Resolution 开始重走,不再需要读取回答原文。
    """

    request_id: uuid.UUID
    run_id: uuid.UUID
    tool_call_id: uuid.UUID | None
    field_paths: tuple[str, ...]
    merged_arguments: dict[str, Any]
    answered_by_actor_id: uuid.UUID | None
    answered_at: datetime


class InputService:
    """input_requests 的应用服务;条件更新与审计在同一事务内完成。"""

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
        field_paths: Sequence[str],
        question: str,
        response_schema: Mapping[str, Any],
        expires_at: datetime,
        tool_call_id: uuid.UUID | None = None,
        options: Mapping[str, Any] | None = None,
        created_by_actor_id: uuid.UUID | None = None,
    ) -> InputRequest:
        """创建 PENDING Input Request;``field_paths`` 是回答的字段白名单。"""
        creator = self._actor_id if created_by_actor_id is None else created_by_actor_id
        paths = [str(path) for path in field_paths]
        if not paths:
            raise ValueError("field_paths must not be empty")
        request = await self._requests.append_input_request(
            workspace_id=self._workspace_id,
            session_id=session_id,
            run_id=run_id,
            field_paths=paths,
            question=question,
            response_schema=dict(response_schema),
            created_by_actor_id=creator,
            expires_at=expires_at,
            tool_call_id=tool_call_id,
            options_json=dict(options) if options is not None else None,
        )
        await self._audit(creator).append(
            INPUT_REQUEST_CREATED,
            payload={"field_paths": paths, "expires_at": expires_at.isoformat()},
            target_type=TARGET_TYPE_INPUT_REQUEST,
            target_id=str(request.id),
            request_id=str(request.id),
            run_id=run_id,
            tool_call_id=tool_call_id,
        )
        return request

    async def answer(
        self,
        request_id: uuid.UUID,
        values: Mapping[str, Any],
        *,
        answered_by_actor_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> InputAnswer:
        """回答 Input Request:校验白名单 → 条件更新 → 合并挂起参数。

        只合并 ``field_paths`` 声明字段;过期(含处理瞬间过期)即标 EXPIRED 并
        以 ``REQUEST_EXPIRED`` 拒绝;非 PENDING 以 ``REQUEST_NOT_PENDING`` 拒绝。
        """
        moment = datetime.now(UTC) if now is None else now
        actor = self._actor_id if answered_by_actor_id is None else answered_by_actor_id
        audit = self._audit(actor)
        request = await self._requests.get_input_request(self._workspace_id, request_id)
        if request is None:
            await self._write_rejection(
                audit, request_id, REQUEST_NOT_FOUND, undeclared=None
            )
            raise InputAnswerRejected(
                REQUEST_NOT_FOUND,
                f"input request {request_id} not found in workspace {self._workspace_id}",
            )
        field_paths = tuple(str(path) for path in request.field_paths)
        undeclared = sorted(set(map(str, values)) - set(field_paths))
        if undeclared:
            await self._write_rejection(
                audit, request_id, UNDECLARED_FIELD, undeclared=undeclared
            )
            raise InputAnswerRejected(
                UNDECLARED_FIELD,
                f"answer fields {undeclared} are not declared in field_paths",
            )
        moved = await self._session.execute(
            sa.update(InputRequest)
            .where(
                InputRequest.id == request_id,
                InputRequest.workspace_id == self._workspace_id,
                InputRequest.status == InputRequestStatus.PENDING,
                InputRequest.expires_at > moment,
            )
            .values(
                status=InputRequestStatus.ANSWERED,
                answered_by_actor_id=actor,
                answered_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        if _rowcount(moved) != 1:
            # 条件更新零行:按当前行状态区分过期与已被处理。
            fresh = await self._requests.get_input_request(
                self._workspace_id, request_id
            )
            if fresh is None:
                await self._write_rejection(
                    audit, request_id, REQUEST_NOT_FOUND, undeclared=None
                )
                raise InputAnswerRejected(REQUEST_NOT_FOUND, "input request vanished")
            if fresh.status == InputRequestStatus.PENDING:
                # 仍 PENDING 即 expires_at <= now:就地过期并拒绝(计划语义)。
                await self._mark_expired(request_id)
                await audit.append(
                    INPUT_REQUEST_EXPIRED,
                    target_type=TARGET_TYPE_INPUT_REQUEST,
                    target_id=str(request_id),
                    request_id=str(request_id),
                    run_id=fresh.run_id,
                    tool_call_id=fresh.tool_call_id,
                )
                await self._write_rejection(
                    audit, request_id, REQUEST_EXPIRED, undeclared=None
                )
                raise InputAnswerRejected(
                    REQUEST_EXPIRED, f"input request {request_id} expired"
                )
            reason = (
                REQUEST_EXPIRED
                if fresh.status == InputRequestStatus.EXPIRED
                else REQUEST_NOT_PENDING
            )
            await self._write_rejection(audit, request_id, reason, undeclared=None)
            raise InputAnswerRejected(
                reason, f"input request {request_id} is {fresh.status.value}"
            )
        await self._session.refresh(request)
        merged = await self._merge_into_pending_arguments(request, values)
        await audit.append(
            INPUT_ANSWERED,
            payload={"field_paths": list(field_paths)},
            target_type=TARGET_TYPE_INPUT_REQUEST,
            target_id=str(request_id),
            request_id=str(request_id),
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
        )
        return InputAnswer(
            request_id=request.id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            field_paths=field_paths,
            merged_arguments=merged,
            answered_by_actor_id=request.answered_by_actor_id,
            answered_at=request.answered_at if request.answered_at is not None else moment,
        )

    async def cancel(
        self,
        request_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> InputRequest:
        """取消 PENDING Input Request(单次消费;过期的请求走 EXPIRED 语义)。"""
        moment = datetime.now(UTC) if now is None else now
        actor = self._actor_id if actor_id is None else actor_id
        audit = self._audit(actor)
        moved = await self._session.execute(
            sa.update(InputRequest)
            .where(
                InputRequest.id == request_id,
                InputRequest.workspace_id == self._workspace_id,
                InputRequest.status == InputRequestStatus.PENDING,
                InputRequest.expires_at > moment,
            )
            .values(status=InputRequestStatus.CANCELLED)
            .execution_options(synchronize_session=False)
        )
        if _rowcount(moved) != 1:
            fresh = await self._requests.get_input_request(
                self._workspace_id, request_id
            )
            if fresh is None:
                raise InputRequestError(
                    REQUEST_NOT_FOUND,
                    f"input request {request_id} not found in workspace {self._workspace_id}",
                )
            if fresh.status == InputRequestStatus.PENDING:
                await self._mark_expired(request_id)
                await audit.append(
                    INPUT_REQUEST_EXPIRED,
                    target_type=TARGET_TYPE_INPUT_REQUEST,
                    target_id=str(request_id),
                    request_id=str(request_id),
                    run_id=fresh.run_id,
                    tool_call_id=fresh.tool_call_id,
                )
                raise InputRequestError(
                    REQUEST_EXPIRED, f"input request {request_id} expired"
                )
            reason = (
                REQUEST_EXPIRED
                if fresh.status == InputRequestStatus.EXPIRED
                else REQUEST_NOT_PENDING
            )
            raise InputRequestError(
                reason, f"input request {request_id} is {fresh.status.value}"
            )
        request = await self._requests.get_input_request(self._workspace_id, request_id)
        if request is None:  # pragma: no cover - 条件更新成功后行必然存在
            raise InputRequestError(REQUEST_NOT_FOUND, "input request vanished")
        await audit.append(
            INPUT_REQUEST_CANCELLED,
            target_type=TARGET_TYPE_INPUT_REQUEST,
            target_id=str(request_id),
            request_id=str(request_id),
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
        )
        return request

    # ---- 内部 ----

    async def _mark_expired(self, request_id: uuid.UUID) -> None:
        """把仍处 PENDING 的请求标 EXPIRED(过期兜底,不带时间条件)。"""
        await self._session.execute(
            sa.update(InputRequest)
            .where(
                InputRequest.id == request_id,
                InputRequest.workspace_id == self._workspace_id,
                InputRequest.status == InputRequestStatus.PENDING,
            )
            .values(status=InputRequestStatus.EXPIRED)
            .execution_options(synchronize_session=False)
        )

    async def _write_rejection(
        self,
        audit: AuditService,
        request_id: uuid.UUID,
        reason_code: str,
        *,
        undeclared: list[str] | None,
    ) -> None:
        """写回答拒绝审计;payload 只含结构性元数据,不含回答值。"""
        payload: dict[str, Any] = {}
        if undeclared is not None:
            payload["undeclared_fields"] = undeclared
        await audit.append(
            INPUT_ANSWER_REJECTED,
            payload=payload,
            outcome=OUTCOME_FAILURE,
            reason_code=reason_code,
            target_type=TARGET_TYPE_INPUT_REQUEST,
            target_id=str(request_id),
            request_id=str(request_id),
        )

    async def _merge_into_pending_arguments(
        self, request: InputRequest, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        """把声明字段合并进 ToolCall 挂起参数并写回;返回合并后参数。

        基准参数取 ``normalized_arguments``(尚未归一化时取 ``raw_arguments``);
        values 已通过白名单校验,因此只有声明字段会进入挂起参数。
        """
        merged: dict[str, Any] = dict(values)
        if request.tool_call_id is None:
            return merged
        tool_call = (
            await self._session.scalars(
                select(ToolCall).where(
                    ToolCall.workspace_id == self._workspace_id,
                    ToolCall.id == request.tool_call_id,
                )
            )
        ).one_or_none()
        if tool_call is None:
            return merged
        base = (
            tool_call.normalized_arguments
            if tool_call.normalized_arguments is not None
            else tool_call.raw_arguments
        )
        merged = {**dict(base), **dict(values)}
        tool_call.normalized_arguments = merged
        return merged


def _rowcount(result: Any) -> int:
    """条件更新影响的行数(与 RunRepository.compare_and_set_status 同构)。"""
    return cast("CursorResult[Any]", result).rowcount
