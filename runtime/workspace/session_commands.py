"""Session/Run/Request 命令应用服务(计划 04 Task 3;架构文档《Runtime执行状态与恢复设计》§4、§5、§6、§9)。

命令语义(架构文档):
- 活动 Run 唯一:同一 Session 同一时刻最多一个非终态 Run(等待输入/确认/回查
  均算活动);提交新用户消息前先查活动 Run,存在即 ``ACTIVE_RUN_EXISTS``(409),
  并发插入由部分唯一索引 ``uq_active_run_per_session`` 兜底;
- 私有 Session:首版仅创建者可读写,非创建者/非成员读取一律 404,不泄漏存在性;
- Request 单次处理:回答/裁决使用 :class:`~runtime.interaction.input_service.InputService`
  与 :class:`~runtime.interaction.confirmation_service.ConfirmationService` 的
  条件更新(PENDING → 目标状态),重复提交以 ``REQUEST_NOT_PENDING``(409)拒绝,
  未声明字段以 ``UNDECLARED_FIELD``(422)拒绝;
- 恢复推进的最小事务:回答/裁决成功后把 Run 从 WAITING_* 经 transitions 合法
  路径迁回 QUEUED 并创建 ADVANCE_RUN Job,由 Worker 领取推进——本服务不调用
  Backend/ToolHandler/RuntimeHost 推进循环;
- 取消 Run 经 ``RunService.transition_run`` 的 CAS + 合法迁移表进入 CANCELLED,
  并尽力取消仍在等待的 Input/Confirmation Request。

与 :class:`~runtime.capabilities.run_factory.RunFactory` 的关系:RunFactory 面向
"解析能力版本 + 创建新 Session + Run + 版本快照"的组合根引导路径,固定绑定
进程内 Registry 组装的 :class:`~runtime.capabilities.assembly.RuntimeAssembly`;
API 进程没有 Registry,这里用"数据库内最新 AgentVersion + 工作区最新
ModelConfigVersion"解析 ``runs`` 必需的两个版本外键,直接经
:class:`~runtime.persistence.repositories.runs.RunRepository` 创建 QUEUED Run,
不写 run_*_versions 快照(版本钉版由 Worker 侧组装路径负责)。

事务边界:与调用方共享同一 AsyncSession,只 flush 不 commit;命令短事务由
路由层在服务返回后统一提交。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.access.actor import ActorContext
from runtime.events.writer import RunNotFoundError
from runtime.execution.run_service import RunService
from runtime.execution.transitions import InvalidTransition
from runtime.interaction.confirmation_service import (
    ConfirmationRequestError,
    ConfirmationService,
)
from runtime.interaction.input_service import InputAnswerRejected, InputRequestError, InputService
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import (
    ACTIVE_JOB_STATUS_VALUES,
    ACTIVE_RUN_STATUS_VALUES,
    AccountStatus,
    ConfirmationRequestStatus,
    InputRequestStatus,
    MessageRole,
    MessageStatus,
    RunJobKind,
)
from runtime.persistence.models.configuration import Agent, AgentVersion, ModelConfigVersion
from runtime.persistence.models.execution import Run, RunJob, ToolCall
from runtime.persistence.models.identity import WorkspaceMembership
from runtime.persistence.models.interaction import (
    ConfirmationRequest,
    InputRequest,
    Message,
    Session,
)
from runtime.persistence.repositories.events import EventRepository
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


# ---- 错误码(与 apps.api.errors 的稳定错误码约定一致) ----

SESSION_NOT_FOUND: Final = "SESSION_NOT_FOUND"
RUN_NOT_FOUND: Final = "RUN_NOT_FOUND"
REQUEST_NOT_FOUND: Final = "REQUEST_NOT_FOUND"
AGENT_NOT_FOUND: Final = "AGENT_NOT_FOUND"
AGENT_VERSION_NOT_FOUND: Final = "AGENT_VERSION_NOT_FOUND"
MODEL_CONFIG_VERSION_NOT_FOUND: Final = "MODEL_CONFIG_VERSION_NOT_FOUND"
ACTIVE_RUN_EXISTS: Final = "ACTIVE_RUN_EXISTS"
INVALID_TRANSITION: Final = "INVALID_TRANSITION"

RUN_CANCELLED_EVENT: Final = "RUN_CANCELLED"
RUN_RESUMED_EVENT: Final = "RUN_RESUMED"

# Request 拒绝原因码 → HTTP 状态码(单次处理语义;未声明字段属请求体错误)。
_REQUEST_ERROR_STATUS: Final[dict[str, int]] = {
    "REQUEST_NOT_FOUND": 404,
    "REQUEST_NOT_PENDING": 409,
    "REQUEST_EXPIRED": 409,
    "UNDECLARED_FIELD": 422,
}

_ACTIVE_RUN_DB_STATUSES: Final[tuple[str, ...]] = ACTIVE_RUN_STATUS_VALUES


class SessionCommandError(RuntimeError):
    """命令被拒绝;``status_code``/``code`` 供路由层翻译为统一错误体。"""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class DispatchedMessage:
    """提交用户消息的结果:消息行与随之创建的 Run 行。"""

    message: Message
    run: Run


@dataclass(frozen=True)
class RunSnapshot:
    """Run 快照:状态、消息、ToolCall 与事件游标。"""

    run: Run
    messages: list[Message]
    tool_calls: list[ToolCall]
    events_cursor: int


@dataclass(frozen=True)
class RequestDecision:
    """Request 单次处理结果。"""

    request_id: uuid.UUID
    kind: str
    status: str
    run_id: uuid.UUID
    run_status: str


class SessionCommandService:
    """Session/Run/Request 命令的编排服务;调用方拥有事务边界。"""

    def __init__(self, session: AsyncSession, *, actor_id: uuid.UUID) -> None:
        self._session = session
        self._actor_id = actor_id
        self._sessions = SessionRepository(session)
        self._runs = RunRepository(session)
        self._jobs = JobRepository(session)
        self._events = EventRepository(session)

    # ---- Session 命令 ----

    async def create_session(self, ctx: ActorContext, *, agent_id: uuid.UUID, title: str | None) -> Session:
        """在 ``ctx.workspace_id`` 创建私有 Session,绑定当前 actor 与成员身份。"""
        agent = await self._session.get(Agent, agent_id)
        if agent is None:
            raise SessionCommandError(
                status_code=404, code=AGENT_NOT_FOUND, message="agent 不存在"
            )
        return await self._sessions.create(
            workspace_id=ctx.workspace_id,
            agent_id=agent_id,
            created_by_actor_id=self._actor_id,
            created_by_membership_id=ctx.membership_id,
            title=title,
        )

    async def get_session(self, session_id: uuid.UUID) -> Session:
        """读取 Session 快照聚合根;仅创建者可读,其余一律 404。"""
        return await self._private_session(session_id)

    async def session_messages(self, session: Session) -> list[Message]:
        """Session 的全部消息,按会话内序号升序。"""
        rows = await self._session.scalars(
            select(Message)
            .where(
                Message.workspace_id == session.workspace_id,
                Message.session_id == session.id,
            )
            .order_by(Message.sequence_no)
        )
        return list(rows)

    async def session_runs(self, session: Session) -> list[Run]:
        """Session 的全部 Run,按创建时间升序。"""
        rows = await self._session.scalars(
            select(Run)
            .where(
                Run.workspace_id == session.workspace_id,
                Run.session_id == session.id,
            )
            .order_by(Run.created_at, Run.id)
        )
        return list(rows)

    async def append_message(
        self,
        session_id: uuid.UUID,
        *,
        content: str,
        model_config_version_id: uuid.UUID | None = None,
    ) -> DispatchedMessage:
        """提交用户消息:消息先落库,再创建 QUEUED Run 与 ADVANCE_RUN Job。

        活动 Run 已存在时返回 409 ``ACTIVE_RUN_EXISTS``;并发撞上
        ``uq_active_run_per_session`` 部分唯一索引时同样翻译为 409。
        """
        session_row = await self._private_session(session_id)
        active_run_id = await self._session.scalar(
            select(Run.id)
            .where(
                Run.workspace_id == session_row.workspace_id,
                Run.session_id == session_row.id,
                Run.status.in_(_ACTIVE_RUN_DB_STATUSES),
            )
            .limit(1)
        )
        if active_run_id is not None:
            raise SessionCommandError(
                status_code=409,
                code=ACTIVE_RUN_EXISTS,
                message="该会话仍有活动 Run,不能提交新用户消息",
            )
        agent_version_id = await self._latest_agent_version_id(session_row.agent_id)
        resolved_model_config_version_id = (
            model_config_version_id
            if model_config_version_id is not None
            else await self._latest_model_config_version_id(session_row.workspace_id)
        )
        if resolved_model_config_version_id is None:
            raise SessionCommandError(
                status_code=409,
                code=MODEL_CONFIG_VERSION_NOT_FOUND,
                message="工作区没有可用的模型配置版本",
            )
        try:
            message = Message(
                workspace_id=session_row.workspace_id,
                session_id=session_row.id,
                run_id=None,
                role=MessageRole.USER,
                status=MessageStatus.COMPLETED,
                content=content,
                sequence_no=await self._next_message_sequence(session_row.id),
            )
            self._session.add(message)
            await self._session.flush()
            run = await self._runs.create(
                workspace_id=session_row.workspace_id,
                session_id=session_row.id,
                created_by_actor_id=self._actor_id,
                created_by_membership_id=session_row.created_by_membership_id,
                agent_version_id=agent_version_id,
                model_config_version_id=resolved_model_config_version_id,
                trigger_message_id=message.id,
            )
            message.run_id = run.id
            await self._session.flush()
            await self._jobs.create(
                workspace_id=session_row.workspace_id,
                run_id=run.id,
                kind=RunJobKind.ADVANCE_RUN,
            )
        except sa.exc.IntegrityError as error:
            if "uq_active_run_per_session" in str(error):
                raise SessionCommandError(
                    status_code=409,
                    code=ACTIVE_RUN_EXISTS,
                    message="该会话仍有活动 Run,不能提交新用户消息",
                ) from error
            raise
        return DispatchedMessage(message=message, run=run)

    # ---- Run 命令与快照 ----

    async def get_run(self, run_id: uuid.UUID) -> RunSnapshot:
        """Run 快照:status/messages/tool_calls/events cursor;越权一律 404。"""
        run = await self._run_in_scope(run_id)
        messages = await self._session.scalars(
            select(Message)
            .where(
                Message.workspace_id == run.workspace_id,
                Message.run_id == run.id,
            )
            .order_by(Message.sequence_no)
        )
        tool_calls = await self._session.scalars(
            select(ToolCall)
            .where(
                ToolCall.workspace_id == run.workspace_id,
                ToolCall.run_id == run.id,
            )
            .order_by(ToolCall.ordinal)
        )
        events = await self._events.list_for_run(
            workspace_id=run.workspace_id, run_id=run.id
        )
        cursor = max((event.sequence_no for event in events), default=0)
        return RunSnapshot(
            run=run,
            messages=list(messages),
            tool_calls=list(tool_calls),
            events_cursor=int(cursor),
        )

    async def cancel_run(self, run_id: uuid.UUID) -> Run:
        """取消 Run:经 transitions 合法路径迁移 CANCELLED 并尽力取消挂起 Request。"""
        run = await self._run_in_scope(run_id)
        try:
            await self._run_service(run.workspace_id).transition_run(
                run_id=run.id,
                target_status=RunStatus.CANCELLED,
                event_kind=RUN_CANCELLED_EVENT,
                event_payload={"reason": "USER_CANCELLED"},
            )
        except InvalidTransition as error:
            raise SessionCommandError(
                status_code=409, code=INVALID_TRANSITION, message=str(error)
            ) from error
        await self._cancel_pending_requests(run)
        fresh = await self._runs.get(run.workspace_id, run.id)
        return run if fresh is None else fresh

    # ---- Request 命令(单次处理) ----

    async def answer_input_request(
        self, request_id: uuid.UUID, values: dict[str, Any]
    ) -> RequestDecision:
        """回答 Input Request:单次消费;成功后把 Run 迁回 QUEUED 并调度推进。"""
        request = await self._input_request_in_scope(request_id)
        try:
            answered = await self._input_service(request.workspace_id).answer(
                request_id, values
            )
        except InputAnswerRejected as error:
            raise _translate_request_error(error) from error
        await self._resume_run(
            request.workspace_id,
            answered.run_id,
            expected_status=RunStatus.WAITING_INPUT,
            payload={"input_request_id": str(request_id)},
        )
        return RequestDecision(
            request_id=request_id,
            kind="INPUT",
            status=InputRequestStatus.ANSWERED.value,
            run_id=answered.run_id,
            run_status=RunStatus.QUEUED.value,
        )

    async def accept_confirmation(self, request_id: uuid.UUID) -> RequestDecision:
        """接受 Confirmation Request:单次消费;成功后把 Run 迁回 QUEUED。"""
        request = await self._confirmation_request_in_scope(request_id)
        try:
            decided = await self._confirmation_service(request.workspace_id).accept(
                request_id
            )
        except ConfirmationRequestError as error:
            raise _translate_request_error(error) from error
        await self._resume_run(
            request.workspace_id,
            decided.run_id,
            expected_status=RunStatus.WAITING_CONFIRMATION,
            payload={"confirmation_id": str(request_id)},
        )
        return RequestDecision(
            request_id=request_id,
            kind="CONFIRMATION",
            status=ConfirmationRequestStatus.ACCEPTED.value,
            run_id=decided.run_id,
            run_status=RunStatus.QUEUED.value,
        )

    async def reject_confirmation(self, request_id: uuid.UUID) -> RequestDecision:
        """拒绝 Confirmation Request:单次消费;REJECTED 不执行 Tool。"""
        request = await self._confirmation_request_in_scope(request_id)
        try:
            decided = await self._confirmation_service(request.workspace_id).reject(
                request_id
            )
        except ConfirmationRequestError as error:
            raise _translate_request_error(error) from error
        await self._resume_run(
            request.workspace_id,
            decided.run_id,
            expected_status=RunStatus.WAITING_CONFIRMATION,
            payload={"confirmation_id": str(request_id), "decision": "REJECTED"},
        )
        return RequestDecision(
            request_id=request_id,
            kind="CONFIRMATION",
            status=ConfirmationRequestStatus.REJECTED.value,
            run_id=decided.run_id,
            run_status=RunStatus.QUEUED.value,
        )

    # ---- 内部:范围解析 ----

    async def _member_workspace_ids(self) -> list[uuid.UUID]:
        """当前 actor 全部有效成员身份的 workspace_id。"""
        rows = await self._session.scalars(
            select(WorkspaceMembership.workspace_id).where(
                WorkspaceMembership.user_id == self._actor_id,
                WorkspaceMembership.status == AccountStatus.ACTIVE,
            )
        )
        return list(rows)

    async def _private_session(self, session_id: uuid.UUID) -> Session:
        """私有 Session 读取:仅创建者且仍在有效成员身份内,否则 404。"""
        row = (
            await self._session.scalars(
                select(Session).where(
                    Session.id == session_id,
                    Session.created_by_actor_id == self._actor_id,
                    Session.workspace_id.in_(await self._member_workspace_ids()),
                )
            )
        ).one_or_none()
        if row is None:
            raise SessionCommandError(
                status_code=404, code=SESSION_NOT_FOUND, message="session 不存在"
            )
        return row

    async def _run_in_scope(self, run_id: uuid.UUID) -> Run:
        """按 actor 成员身份范围读取 Run;不存在或不属于这些工作区一律 404。"""
        row = (
            await self._session.scalars(
                select(Run).where(
                    Run.id == run_id,
                    Run.workspace_id.in_(await self._member_workspace_ids()),
                )
            )
        ).one_or_none()
        if row is None:
            raise SessionCommandError(
                status_code=404, code=RUN_NOT_FOUND, message="run 不存在"
            )
        return row

    async def _input_request_in_scope(self, request_id: uuid.UUID) -> InputRequest:
        row = (
            await self._session.scalars(
                select(InputRequest).where(
                    InputRequest.id == request_id,
                    InputRequest.workspace_id.in_(await self._member_workspace_ids()),
                )
            )
        ).one_or_none()
        if row is None:
            raise SessionCommandError(
                status_code=404, code=REQUEST_NOT_FOUND, message="request 不存在"
            )
        return row

    async def _confirmation_request_in_scope(
        self, request_id: uuid.UUID
    ) -> ConfirmationRequest:
        row = (
            await self._session.scalars(
                select(ConfirmationRequest).where(
                    ConfirmationRequest.id == request_id,
                    ConfirmationRequest.workspace_id.in_(
                        await self._member_workspace_ids()
                    ),
                )
            )
        ).one_or_none()
        if row is None:
            raise SessionCommandError(
                status_code=404, code=REQUEST_NOT_FOUND, message="request 不存在"
            )
        return row

    # ---- 内部:版本解析与推进 ----

    async def _latest_agent_version_id(self, agent_id: uuid.UUID) -> uuid.UUID:
        """数据库内该 Agent 的最新版本(无进程内 Registry 时的最小钉版语义)。"""
        version_id = await self._session.scalar(
            select(AgentVersion.id)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc(), AgentVersion.id.desc())
            .limit(1)
        )
        if version_id is None:
            raise SessionCommandError(
                status_code=409,
                code=AGENT_VERSION_NOT_FOUND,
                message="agent 没有可用版本",
            )
        return version_id

    async def _latest_model_config_version_id(
        self, workspace_id: uuid.UUID
    ) -> uuid.UUID | None:
        """工作区内最新模型配置版本;显式传入时由调用方提供。"""
        return await self._session.scalar(
            select(ModelConfigVersion.id)
            .where(ModelConfigVersion.workspace_id == workspace_id)
            .order_by(ModelConfigVersion.version.desc(), ModelConfigVersion.id.desc())
            .limit(1)
        )

    async def _next_message_sequence(self, session_id: uuid.UUID) -> int:
        """会话内消息序号:max+1(uq_messages_session_sequence)。"""
        current = await self._session.scalar(
            select(func.max(Message.sequence_no)).where(Message.session_id == session_id)
        )
        return 1 if current is None else int(current) + 1

    def _run_service(self, workspace_id: uuid.UUID) -> RunService:
        return RunService(session=self._session, workspace_id=workspace_id)

    def _input_service(self, workspace_id: uuid.UUID) -> InputService:
        return InputService(
            self._session, workspace_id=workspace_id, actor_id=self._actor_id
        )

    def _confirmation_service(self, workspace_id: uuid.UUID) -> ConfirmationService:
        return ConfirmationService(
            self._session, workspace_id=workspace_id, actor_id=self._actor_id
        )

    async def _resume_run(
        self,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        expected_status: RunStatus,
        payload: dict[str, Any],
    ) -> None:
        """恢复推进的最小事务:WAITING_* → QUEUED + ADVANCE_RUN Job。

        不调用 Backend/ToolHandler/RuntimeHost 推进循环;Worker 领取 Job 后
        从 Tool Resolution 重走完整 PreToolUse(架构文档 §5、§6)。
        """
        try:
            await self._run_service(workspace_id).transition_run(
                run_id=run_id,
                target_status=RunStatus.QUEUED,
                event_kind=RUN_RESUMED_EVENT,
                event_payload=payload,
                expected_status=expected_status,
            )
        except InvalidTransition as error:
            raise SessionCommandError(
                status_code=409, code=INVALID_TRANSITION, message=str(error)
            ) from error
        except RunNotFoundError as error:  # pragma: no cover - 范围查询已确认存在
            raise SessionCommandError(
                status_code=404, code=RUN_NOT_FOUND, message=str(error)
            ) from error
        await self._ensure_advance_job(workspace_id, run_id)

    async def _ensure_advance_job(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """确保该 Run 恰有一个活动 ADVANCE_RUN Job(uq_active_job_per_run)。

        上一轮 Job 仍在 PENDING/LEASED 时复用,不重复创建;已被 Worker 消费
        (DONE/DEAD)才创建新 Job。重复提交 Request 的幂等语义据此保持
        "不重复创建 Job"(架构文档 §4.4)。
        """
        active_job_id = await self._session.scalar(
            select(RunJob.id)
            .where(
                RunJob.workspace_id == workspace_id,
                RunJob.run_id == run_id,
                RunJob.status.in_(ACTIVE_JOB_STATUS_VALUES),
            )
            .limit(1)
        )
        if active_job_id is not None:
            return
        await self._jobs.create(
            workspace_id=workspace_id, run_id=run_id, kind=RunJobKind.ADVANCE_RUN
        )

    async def _cancel_pending_requests(self, run: Run) -> None:
        """尽力取消仍在等待的 Input/Confirmation Request(取消 Run 的从属动作)。"""
        input_ids = list(
            await self._session.scalars(
                select(InputRequest.id).where(
                    InputRequest.workspace_id == run.workspace_id,
                    InputRequest.run_id == run.id,
                    InputRequest.status == InputRequestStatus.PENDING,
                )
            )
        )
        for request_id in input_ids:
            try:
                await self._input_service(run.workspace_id).cancel(request_id)
            except InputRequestError:  # 过期竞态:保留取消 Run 的主结果。
                continue
        confirmation_ids = list(
            await self._session.scalars(
                select(ConfirmationRequest.id).where(
                    ConfirmationRequest.workspace_id == run.workspace_id,
                    ConfirmationRequest.run_id == run.id,
                    ConfirmationRequest.status == ConfirmationRequestStatus.PENDING,
                )
            )
        )
        for request_id in confirmation_ids:
            try:
                await self._confirmation_service(run.workspace_id).cancel(request_id)
            except ConfirmationRequestError:  # 过期竞态:保留取消 Run 的主结果。
                continue


def _translate_request_error(
    error: InputAnswerRejected | ConfirmationRequestError,
) -> SessionCommandError:
    """Request 领域错误 → 稳定错误码与状态码(未声明字段属 422 请求体错误)。"""
    return SessionCommandError(
        status_code=_REQUEST_ERROR_STATUS.get(error.reason_code, 409),
        code=error.reason_code,
        message=str(error),
    )
