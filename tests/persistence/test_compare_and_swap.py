"""Run 状态 CAS 与 Job 租约条件更新测试(计划 01 Task 4 Step 1)。

CAS UPDATE 必须同时匹配 workspace_id、state_version 与(提供时)当前状态
(架构文档 §13);成功时 state_version 原递增。Job 完成必须以 lease token
阻止失租 Worker 提交(架构文档 §9)。
"""

import uuid
from datetime import timedelta

from runtime.persistence.base import RunJobKind, RunJobStatus, RunStatus
from runtime.persistence.repositories.jobs import JobRepository


async def test_stale_run_version_cannot_commit(run_repository, seeded_run) -> None:
    updated = await run_repository.compare_and_set_status(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        expected_version=seeded_run.state_version + 1,
        new_status="RUNNING",
    )
    assert updated is False


async def test_cas_succeeds_on_matching_version_and_status_and_increments_state_version(
    run_repository, seeded_run
) -> None:
    expected_version = seeded_run.state_version
    updated = await run_repository.compare_and_set_status(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        expected_version=expected_version,
        new_status="RUNNING",
        expected_status="QUEUED",
    )
    assert updated is True

    refreshed = await run_repository.get(
        workspace_id=seeded_run.workspace_id, run_id=seeded_run.id
    )
    assert refreshed is not None
    assert refreshed.status is RunStatus.RUNNING
    assert refreshed.state_version == expected_version + 1


async def test_cas_returns_false_on_status_mismatch(run_repository, seeded_run) -> None:
    expected_version = seeded_run.state_version
    updated = await run_repository.compare_and_set_status(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        expected_version=expected_version,
        new_status="RUNNING",
        expected_status="RUNNING",  # 种子 Run 处于 QUEUED,状态不匹配
    )
    assert updated is False

    refreshed = await run_repository.get(
        workspace_id=seeded_run.workspace_id, run_id=seeded_run.id
    )
    assert refreshed is not None
    assert refreshed.status is RunStatus.QUEUED
    assert refreshed.state_version == expected_version


async def test_cas_with_wrong_workspace_returns_false(run_repository, seeded_run) -> None:
    updated = await run_repository.compare_and_set_status(
        workspace_id=uuid.uuid4(),
        run_id=seeded_run.id,
        expected_version=seeded_run.state_version,
        new_status="RUNNING",
    )
    assert updated is False


async def test_job_lease_claim_and_fenced_completion(
    jobs_repository: JobRepository, seeded_run
) -> None:
    job = await jobs_repository.create(
        workspace_id=seeded_run.workspace_id,
        run_id=seeded_run.id,
        kind=RunJobKind.ADVANCE_RUN,
    )

    claimed = await jobs_repository.claim_due_job(
        worker_id="worker-1", lease_duration=timedelta(minutes=1)
    )
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status is RunJobStatus.LEASED
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_token is not None
    assert claimed.attempt_count == 1

    # 错误 lease token:失租 Worker 不能提交。
    assert (
        await jobs_repository.complete(
            workspace_id=seeded_run.workspace_id,
            job_id=job.id,
            lease_token=uuid.uuid4(),
        )
        is False
    )
    assert (
        await jobs_repository.complete(
            workspace_id=seeded_run.workspace_id,
            job_id=job.id,
            lease_token=claimed.lease_token,
        )
        is True
    )
    # 已完成的 Job 不能再次完成。
    assert (
        await jobs_repository.complete(
            workspace_id=seeded_run.workspace_id,
            job_id=job.id,
            lease_token=claimed.lease_token,
        )
        is False
    )
