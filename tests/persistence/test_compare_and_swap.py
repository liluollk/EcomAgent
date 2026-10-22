"""Run 状态 CAS 测试(计划 01 Task 4 Step 1;ADR-003)。

CAS UPDATE 必须同时匹配 workspace_id、state_version 与(提供时)当前状态
(架构文档 §13);成功时 state_version 原子递增。首版没有 Job 租约,
Run 的并发写保护只剩这一层,因此这些用例是执行模型的底线。
"""

import uuid

from runtime.persistence.base import RunStatus


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
