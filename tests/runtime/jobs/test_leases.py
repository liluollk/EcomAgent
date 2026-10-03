"""RunJob 租约、续租与 fencing token 测试(计划 02 Task 4;架构文档 §8、§13)。

覆盖:
- 并发领取:两个独立连接/事务同时 claim,恰一个拿到租约;
- fencing:过期/伪造 token 不能完成 Job,当前 token 才能完成并收尾 Attempt;
- 心跳:仅当前 token 可续租且 ``lease_expires_at`` 后移;
- Sweeper:过期租约回收(Attempt → ABANDONED),按 Operation 状态决定
  下一 Job 为 RECONCILE_OPERATION 或回到 PENDING;
- Worker:claim → 回调 → complete 最小骨架与异常释放语义。
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from runtime.jobs.lease_service import Lease, LeaseOutcome, LeaseService
from runtime.jobs.sweeper import LeaseSweeper
from runtime.jobs.worker import RunJobWorker
from runtime.persistence.base import (
    ConfigScope,
    MembershipRole,
    MessageRole,
    MessageStatus,
    ModelBackendType,
    OperationStatus,
    RunAttemptStatus,
    RunJobKind,
    RunJobStatus,
    RunStatus,
    ToolEffect,
)
from runtime.persistence.models.configuration import (
    Agent,
    AgentVersion,
    ModelConfigVersion,
    ModelConnection,
    ToolDefinition,
    ToolVersion,
)
from runtime.persistence.models.execution import (
    Operation,
    Run,
    RunAttempt,
    RunJob,
    ToolCall,
)
from runtime.persistence.models.identity import User, Workspace, WorkspaceMembership
from runtime.persistence.models.interaction import Message
from runtime.persistence.models.interaction import Session as SessionRow
from runtime.persistence.repositories.jobs import JobRepository
from runtime.persistence.repositories.runs import RunRepository
from runtime.persistence.repositories.sessions import SessionRepository


# ---------------------------------------------------------------------------
# 提交式种子与清理(跨连接并发/Worker 测试需要已提交数据;参考 test_writer.py)
# ---------------------------------------------------------------------------


async def _seed_committed_job(engine: AsyncEngine) -> dict[str, uuid.UUID]:
    """提交最小种子链(workspace/.../run)与一个 PENDING ADVANCE_RUN Job。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
            user = User(id=uuid.uuid4(), login=f"user-{uuid.uuid4()}", password_hash="test-hash")
            membership = WorkspaceMembership(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                user_id=user.id,
                role=MembershipRole.OPERATOR,
            )
            agent = Agent(id=uuid.uuid4(), stable_key=f"agent-{uuid.uuid4()}", name="test-agent")
            agent_version = AgentVersion(
                id=uuid.uuid4(),
                agent_id=agent.id,
                version=1,
                definition_json={},
                definition_hash="test-hash",
            )
            model_connection = ModelConnection(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                stable_key=f"model-{uuid.uuid4()}",
                name="test-model",
            )
            model_config_version = ModelConfigVersion(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                model_connection_id=model_connection.id,
                version=1,
                backend_type=ModelBackendType.OPENAI,
                base_url="https://api.example.com/v1",
                model="gpt-test",
                non_secret_options={},
                config_hash="test-hash",
            )
            # 按外键依赖分批 flush,保证被引用行先落库。
            session.add_all([workspace, user, agent])
            await session.flush()
            session.add_all([membership, agent_version, model_connection])
            await session.flush()
            session.add(model_config_version)
            await session.flush()
            session_row = await SessionRepository(session).create(
                workspace_id=workspace.id,
                agent_id=agent.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
            )
            run = await RunRepository(session).create(
                workspace_id=workspace.id,
                session_id=session_row.id,
                created_by_actor_id=user.id,
                created_by_membership_id=membership.id,
                agent_version_id=agent_version.id,
                model_config_version_id=model_config_version.id,
            )
            job = await JobRepository(session).create(
                workspace_id=workspace.id,
                run_id=run.id,
                kind=RunJobKind.ADVANCE_RUN,
            )
            return {
                "workspace_id": workspace.id,
                "user_id": user.id,
                "membership_id": membership.id,
                "agent_id": agent.id,
                "agent_version_id": agent_version.id,
                "model_connection_id": model_connection.id,
                "model_config_version_id": model_config_version.id,
                "run_id": run.id,
                "job_id": job.id,
            }
    finally:
        await session.close()


async def _cleanup_committed_job(engine: AsyncEngine, ids: dict[str, uuid.UUID]) -> None:
    """按外键依赖倒序删除已提交的种子行,保持测试库干净。"""
    workspace_id = ids["workspace_id"]
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await session.execute(sa.delete(RunAttempt).where(RunAttempt.run_id == ids["run_id"]))
            await session.execute(sa.delete(RunJob).where(RunJob.workspace_id == workspace_id))
            await session.execute(sa.delete(Run).where(Run.workspace_id == workspace_id))
            await session.execute(sa.delete(SessionRow).where(SessionRow.workspace_id == workspace_id))
            await session.execute(
                sa.delete(ModelConfigVersion).where(
                    ModelConfigVersion.id == ids["model_config_version_id"]
                )
            )
            await session.execute(
                sa.delete(ModelConnection).where(
                    ModelConnection.id == ids["model_connection_id"]
                )
            )
            await session.execute(
                sa.delete(AgentVersion).where(AgentVersion.id == ids["agent_version_id"])
            )
            await session.execute(
                sa.delete(WorkspaceMembership).where(
                    WorkspaceMembership.id == ids["membership_id"]
                )
            )
            await session.execute(sa.delete(Agent).where(Agent.id == ids["agent_id"]))
            await session.execute(sa.delete(User).where(User.id == ids["user_id"]))
            await session.execute(sa.delete(Workspace).where(Workspace.id == workspace_id))
    finally:
        await session.close()


async def _seed_uncertain_operation(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    status: OperationStatus,
) -> None:
    """为 Run 造一条处于 EXECUTING/UNKNOWN 的写 Operation(tool_call 链)。"""
    tool_definition = ToolDefinition(
        id=uuid.uuid4(),
        scope=ConfigScope.GLOBAL,
        workspace_id=None,
        stable_key=f"tool-{uuid.uuid4()}",
        name="test-tool",
    )
    tool_version = ToolVersion(
        id=uuid.uuid4(),
        workspace_id=None,
        tool_id=tool_definition.id,
        version=1,
        effect=ToolEffect.WRITE,
        input_schema={},
        output_schema={},
        schema_hash="test-hash",
    )
    message = Message(
        workspace_id=workspace_id,
        session_id=session_id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETED,
        content="trigger",
        sequence_no=1,
    )
    session.add_all([tool_definition, tool_version, message])
    await session.flush()
    tool_call = ToolCall(
        workspace_id=workspace_id,
        run_id=run_id,
        message_id=message.id,
        tool_version_id=tool_version.id,
        ordinal=1,
        raw_arguments={},
    )
    session.add(tool_call)
    await session.flush()
    session.add(
        Operation(
            workspace_id=workspace_id,
            tool_call_id=tool_call.id,
            operation_id=uuid.uuid4(),
            idempotency_key=f"op-{uuid.uuid4()}",
            adapter_key="test-adapter",
            status=status,
        )
    )
    await session.flush()


async def _latest_attempt(db_session: AsyncSession, run_id: uuid.UUID) -> RunAttempt:
    stmt = (
        select(RunAttempt)
        .where(RunAttempt.run_id == run_id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    )
    attempt = (await db_session.scalars(stmt)).one()
    return attempt


# ---------------------------------------------------------------------------
# 领取与 fencing
# ---------------------------------------------------------------------------


async def test_only_one_worker_leases_a_job(engine: AsyncEngine) -> None:
    """计划原文:两个 Worker 并发 claim,恰好一个拿到租约。

    单一 AsyncSession(单连接)下 asyncio.gather 实际串行执行,故用两个
    独立连接/事务实现真实竞争(参考 events/test_writer.py 的做法)。
    """
    ids = await _seed_committed_job(engine)
    try:

        async def worker(worker_id: str) -> Lease | None:
            session = AsyncSession(engine, expire_on_commit=False)
            try:
                async with session.begin():
                    return await LeaseService(session).claim(worker_id)
            finally:
                await session.close()

        leases = await asyncio.gather(worker("worker-a"), worker("worker-b"))
        assert sum(lease is not None for lease in leases) == 1
    finally:
        await _cleanup_committed_job(engine, ids)


async def test_stale_lease_token_cannot_commit(
    lease_service: LeaseService, expired_lease: Lease
) -> None:
    """计划原文:租约过期后用 stale token complete,必须被 fencing 拒绝。"""
    committed = await lease_service.complete(expired_lease.job_id, "stale-token")
    assert committed is False


async def test_claim_creates_attempt_token_and_moves_run_to_running(
    lease_service: LeaseService, pending_job: RunJob, seeded_run: Run, db_session: AsyncSession
) -> None:
    """claim 生成随机 token、创建 RunAttempt,并把 Run 从 QUEUED CAS 到 RUNNING。"""
    lease = await lease_service.claim("worker-a")

    assert lease is not None
    assert lease.job_id == pending_job.id
    assert lease.run_id == seeded_run.id
    assert isinstance(lease.lease_token, uuid.UUID)
    assert lease.lease_expires_at is not None

    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.LEASED
    assert pending_job.lease_owner == "worker-a"
    assert pending_job.lease_token == lease.lease_token
    assert pending_job.attempt_count == 1

    await db_session.refresh(seeded_run)
    assert seeded_run.status is RunStatus.RUNNING
    assert seeded_run.state_version == 2

    attempt = await _latest_attempt(db_session, seeded_run.id)
    assert attempt.attempt_no == 1
    assert attempt.worker_id == "worker-a"
    assert attempt.status is RunAttemptStatus.RUNNING
    assert attempt.started_at is not None
    assert attempt.finished_at is None


async def test_claimed_job_cannot_be_claimed_again_in_same_session(
    lease_service: LeaseService, pending_job: RunJob
) -> None:
    """单连接语义:Job 领取后不再是 PENDING,第二个 claim 拿不到。"""
    first = await lease_service.claim("worker-a")
    second = await lease_service.claim("worker-b")
    assert first is not None
    assert second is None
    assert pending_job.status is RunJobStatus.LEASED


async def test_heartbeat_extends_lease_with_current_token(
    lease_service: LeaseService, pending_job: RunJob, db_session: AsyncSession
) -> None:
    """心跳:当前 token 续租成功且 lease_expires_at 后移;旧 token 续租失败。"""
    lease = await lease_service.claim("worker-a")
    assert lease is not None
    original_expiry = pending_job.lease_expires_at
    assert original_expiry is not None

    renewed = await lease_service.heartbeat(
        pending_job.id, lease.lease_token, lease_duration=timedelta(seconds=120)
    )
    assert renewed is True
    await db_session.refresh(pending_job)
    assert pending_job.lease_expires_at is not None
    assert pending_job.lease_expires_at > original_expiry

    stale = await lease_service.heartbeat(pending_job.id, uuid.uuid4())
    assert stale is False
    await db_session.refresh(pending_job)
    assert pending_job.lease_expires_at is not None
    assert pending_job.lease_expires_at > original_expiry


async def test_complete_with_current_token_finalizes_job_and_attempt(
    lease_service: LeaseService, pending_job: RunJob, seeded_run: Run, db_session: AsyncSession
) -> None:
    """当前 token complete → True;Job 终态化为 DONE,RunAttempt 收尾 SUCCEEDED。"""
    lease = await lease_service.claim("worker-a")
    assert lease is not None

    committed = await lease_service.complete(pending_job.id, lease.lease_token)
    assert committed is True

    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.DONE
    attempt = await _latest_attempt(db_session, seeded_run.id)
    assert attempt.status is RunAttemptStatus.SUCCEEDED
    assert attempt.finished_at is not None

    # fencing:Job 已 DONE,第二次 complete 不再命中。
    assert await lease_service.complete(pending_job.id, lease.lease_token) is False


async def test_complete_with_failure_code_dead_letters_job(
    lease_service: LeaseService, pending_job: RunJob, seeded_run: Run, db_session: AsyncSession
) -> None:
    """确定性失败的 outcome → Job DEAD,Attempt FAILED 并记录 failure_code。"""
    lease = await lease_service.claim("worker-a")
    assert lease is not None

    committed = await lease_service.complete(
        pending_job.id, lease.lease_token, LeaseOutcome(failure_code="INVALID_REQUEST")
    )
    assert committed is True

    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.DEAD
    assert pending_job.last_error_code == "INVALID_REQUEST"
    attempt = await _latest_attempt(db_session, seeded_run.id)
    assert attempt.status is RunAttemptStatus.FAILED
    assert attempt.failure_code == "INVALID_REQUEST"
    assert attempt.finished_at is not None


# ---------------------------------------------------------------------------
# Sweeper 租约回收
# ---------------------------------------------------------------------------


async def test_sweeper_requeues_expired_lease_and_abandons_attempt(
    sweeper: LeaseSweeper,
    lease_service: LeaseService,
    expired_lease: Lease,
    pending_job: RunJob,
    seeded_run: Run,
    db_session: AsyncSession,
) -> None:
    """无不确定 Operation 时:Attempt ABANDONED,Job 回 PENDING,Run 回 QUEUED。"""
    recovered = await sweeper.sweep_expired()
    assert recovered == 1

    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.PENDING
    assert pending_job.lease_owner is None
    assert pending_job.lease_token is None
    assert pending_job.lease_expires_at is None
    await db_session.refresh(seeded_run)
    assert seeded_run.status is RunStatus.QUEUED
    attempt = await _latest_attempt(db_session, seeded_run.id)
    assert attempt.status is RunAttemptStatus.ABANDONED
    assert attempt.finished_at is not None

    # 回收后的 Job 可再次被领取,并创建新 Attempt(attempt_no=2)。
    repossessed = await lease_service.claim("worker-b")
    assert repossessed is not None
    next_attempt = await _latest_attempt(db_session, seeded_run.id)
    assert next_attempt.attempt_no == 2


async def test_sweeper_creates_reconcile_job_when_operation_uncertain(
    sweeper: LeaseSweeper,
    lease_service: LeaseService,
    expired_lease: Lease,
    pending_job: RunJob,
    seeded_run: Run,
    seeded_session: SessionRow,
    db_session: AsyncSession,
) -> None:
    """Operation 处于 EXECUTING/UNKNOWN 时:下一 Job 必须是 RECONCILE_OPERATION。

    ARCHITECTURE:不能直接再次执行;Run 进入 WAITING_RECONCILIATION,
    领取该 RECONCILE_OPERATION Job 时 Run 保持该状态。
    """
    await _seed_uncertain_operation(
        db_session,
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_session.id,
        run_id=seeded_run.id,
        status=OperationStatus.EXECUTING,
    )

    recovered = await sweeper.sweep_expired()
    assert recovered == 1

    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.PENDING
    assert pending_job.kind is RunJobKind.RECONCILE_OPERATION
    assert pending_job.lease_token is None
    await db_session.refresh(seeded_run)
    assert seeded_run.status is RunStatus.WAITING_RECONCILIATION

    reconcile_lease = await lease_service.claim("worker-b")
    assert reconcile_lease is not None
    await db_session.refresh(seeded_run)
    assert seeded_run.status is RunStatus.WAITING_RECONCILIATION
    next_attempt = await _latest_attempt(db_session, seeded_run.id)
    assert next_attempt.attempt_no == 2


async def test_sweeper_keeps_reconcile_kind_when_operation_unknown(
    sweeper: LeaseSweeper,
    lease_service: LeaseService,
    expired_lease: Lease,
    pending_job: RunJob,
    seeded_run: Run,
    seeded_session: SessionRow,
    db_session: AsyncSession,
) -> None:
    """RECONCILE_OPERATION Job 过期且回查仍 UNKNOWN:kind 保持不变,Run 原地等待。"""
    # 第一次回收:Operation EXECUTING → ADVANCE_RUN 改判为 RECONCILE_OPERATION。
    await _seed_uncertain_operation(
        db_session,
        workspace_id=seeded_run.workspace_id,
        session_id=seeded_session.id,
        run_id=seeded_run.id,
        status=OperationStatus.EXECUTING,
    )
    assert await sweeper.sweep_expired() == 1
    await db_session.refresh(pending_job)
    assert pending_job.kind is RunJobKind.RECONCILE_OPERATION

    # 领取回查 Job 后再次失租,且 Operation 已变为 UNKNOWN。
    lease = await lease_service.claim("worker-b")
    assert lease is not None
    pending_job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    op_stmt = select(Operation).where(Operation.workspace_id == seeded_run.workspace_id)
    operation = (await db_session.scalars(op_stmt)).one()
    operation.status = OperationStatus.UNKNOWN
    await db_session.flush()

    assert await sweeper.sweep_expired() == 1
    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.PENDING
    assert pending_job.kind is RunJobKind.RECONCILE_OPERATION
    await db_session.refresh(seeded_run)
    assert seeded_run.status is RunStatus.WAITING_RECONCILIATION
    attempt = await _latest_attempt(db_session, seeded_run.id)
    assert attempt.status is RunAttemptStatus.ABANDONED
    assert attempt.attempt_no == 2


async def test_sweeper_ignores_unexpired_leases(
    sweeper: LeaseSweeper,
    lease_service: LeaseService,
    pending_job: RunJob,
    db_session: AsyncSession,
) -> None:
    """未过期的 LEASED Job 不被回收。"""
    lease = await lease_service.claim("worker-a")
    assert lease is not None
    assert await sweeper.sweep_expired() == 0
    await db_session.refresh(pending_job)
    assert pending_job.status is RunJobStatus.LEASED


# ---------------------------------------------------------------------------
# Worker 最小骨架
# ---------------------------------------------------------------------------


async def test_worker_completes_job_after_handler_success(engine: AsyncEngine) -> None:
    """Worker 循环:claim → 执行回调 → complete;Job DONE、Attempt SUCCEEDED。"""
    ids = await _seed_committed_job(engine)
    try:
        seen_leases: list[Lease] = []

        async def handler(lease: Lease) -> LeaseOutcome | None:
            seen_leases.append(lease)
            return None

        worker = RunJobWorker(
            session_factory=partial(AsyncSession, engine, expire_on_commit=False),
            handler=handler,
            worker_id="worker-a",
        )
        assert await worker.run_once() is True
        assert await worker.run_once() is False
        assert len(seen_leases) == 1
        assert seen_leases[0].job_id == ids["job_id"]

        session = AsyncSession(engine, expire_on_commit=False)
        try:
            job = await session.get(RunJob, ids["job_id"])
            assert job is not None and job.status is RunJobStatus.DONE
            attempts = (
                await session.scalars(select(RunAttempt).where(RunAttempt.run_id == ids["run_id"]))
            ).all()
            assert [attempt.status for attempt in attempts] == [RunAttemptStatus.SUCCEEDED]
        finally:
            await session.close()
    finally:
        await _cleanup_committed_job(engine, ids)


async def test_worker_releases_job_when_handler_raises(engine: AsyncEngine) -> None:
    """回调异常:Job 释放回 PENDING(可重试),Attempt FAILED 并写入错误码。"""
    ids = await _seed_committed_job(engine)
    try:

        async def failing_handler(lease: Lease) -> LeaseOutcome | None:
            raise RuntimeError("boom")

        worker = RunJobWorker(
            session_factory=partial(AsyncSession, engine, expire_on_commit=False),
            handler=failing_handler,
            worker_id="worker-a",
        )
        assert await worker.run_once() is True

        session = AsyncSession(engine, expire_on_commit=False)
        try:
            job = await session.get(RunJob, ids["job_id"])
            assert job is not None and job.status is RunJobStatus.PENDING
            assert job.last_error_code == "WORKER_ERROR"
            assert job.lease_token is None
            attempts = (
                await session.scalars(select(RunAttempt).where(RunAttempt.run_id == ids["run_id"]))
            ).all()
            assert [attempt.status for attempt in attempts] == [RunAttemptStatus.FAILED]

            # 释放后的 Job 可再次领取(新事务,避免 now() 停留在旧事务起点)。
            claim_session = AsyncSession(engine, expire_on_commit=False)
            try:
                async with claim_session.begin():
                    lease = await LeaseService(claim_session).claim("worker-b")
                assert lease is not None
            finally:
                await claim_session.close()
        finally:
            await session.close()
    finally:
        await _cleanup_committed_job(engine, ids)
