"""Input/Confirmation Request Repository:追加与 workspace 范围查询(架构文档 §8)。

每个 ToolCall 同时最多一个 PENDING Input/Confirmation Request,由部分唯一索引兜底;
并发回答的条件更新(``status = 'PENDING' AND expires_at > now()``)属于应用服务事务,
由后续任务基于本 Repository 的行对象实现。
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.base import ConfirmationRequestStatus, InputRequestStatus
from runtime.persistence.models.interaction import ConfirmationRequest, InputRequest


class RequestRepository:
    """input_requests 与 confirmation_requests 的持久化操作;只 flush,不提交事务。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_input_request(
        self,
        *,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        field_paths: list[Any],
        question: str,
        response_schema: dict[str, Any],
        created_by_actor_id: uuid.UUID,
        expires_at: datetime,
        tool_call_id: uuid.UUID | None = None,
        options_json: dict[str, Any] | None = None,
        status: InputRequestStatus = InputRequestStatus.PENDING,
    ) -> InputRequest:
        """追加 Input Request;``field_paths``/``response_schema`` 必须相互一致。"""
        request = InputRequest(
            workspace_id=workspace_id,
            session_id=session_id,
            run_id=run_id,
            field_paths=field_paths,
            question=question,
            response_schema=response_schema,
            created_by_actor_id=created_by_actor_id,
            expires_at=expires_at,
            tool_call_id=tool_call_id,
            options_json=options_json,
            status=status,
        )
        self._session.add(request)
        await self._session.flush()
        return request

    async def get_input_request(
        self, workspace_id: uuid.UUID, request_id: uuid.UUID
    ) -> InputRequest | None:
        """按 (workspace_id, request_id) 读取;错误 workspace 返回 None。"""
        stmt = (
            select(InputRequest)
            .where(InputRequest.workspace_id == workspace_id, InputRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()

    async def append_confirmation_request(
        self,
        *,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        run_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        actor_id: uuid.UUID,
        fingerprint: str,
        normalized_arguments_hash: str,
        resource_versions: dict[str, Any],
        expires_at: datetime,
        operation_id: uuid.UUID | None = None,
        tool_schema_version: int | None = None,
        policy_version: int | None = None,
        execution_mode_version: int | None = None,
        status: ConfirmationRequestStatus = ConfirmationRequestStatus.PENDING,
    ) -> ConfirmationRequest:
        """追加写操作 Confirmation Request;指纹与哈希用于检测确认前状态变化。"""
        request = ConfirmationRequest(
            workspace_id=workspace_id,
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            actor_id=actor_id,
            fingerprint=fingerprint,
            normalized_arguments_hash=normalized_arguments_hash,
            resource_versions=resource_versions,
            tool_schema_version=tool_schema_version,
            policy_version=policy_version,
            execution_mode_version=execution_mode_version,
            status=status,
            expires_at=expires_at,
        )
        self._session.add(request)
        await self._session.flush()
        return request

    async def get_confirmation_request(
        self, workspace_id: uuid.UUID, request_id: uuid.UUID
    ) -> ConfirmationRequest | None:
        """按 (workspace_id, request_id) 读取;错误 workspace 返回 None。"""
        stmt = (
            select(ConfirmationRequest)
            .where(ConfirmationRequest.workspace_id == workspace_id, ConfirmationRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.scalars(stmt)).one_or_none()
