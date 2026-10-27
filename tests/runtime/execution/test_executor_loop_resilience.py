"""执行器后台循环的抗故障能力。

这几条针对的是一个静默故障:队列探测(``_next_queued`` 的 SELECT)原先在
异常边界之外,数据库连接抖动一次就会让 ``_loop`` 以异常终止——进程还活着、
``/api/health`` 仍返回 200,但所有 Run 永远停在 QUEUED,只能重启恢复。

锁定的行为:探测失败只让本轮失败,循环继续;失败次数可观测;关闭路径不被
残留异常打断。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest

from runtime.execution.executor import RunExecutor
from runtime.interfaces.state import RunStatus


class _EmptyRows:
    def __iter__(self):
        return iter([])


class _NullContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FailingProbeSession:
    """恢复扫描可用(空结果),队列探测一律失败:模拟连接池抖动。"""

    def __init__(self) -> None:
        self.probe_attempts = 0

    async def scalars(self, stmt):
        return _EmptyRows()

    async def scalar(self, stmt):
        self.probe_attempts += 1
        raise RuntimeError("connection reset by peer")

    async def execute(self, stmt):
        self.probe_attempts += 1
        raise RuntimeError("connection reset by peer")

    async def close(self) -> None:
        return None

    def begin(self):
        return _NullContext()


class _IdleDriver:
    """不应被调用:队列探测始终失败,没有 Run 可领取。"""

    async def advance(self, claim, *, answered=None):
        raise AssertionError("no run should be claimed")

    async def reconcile(self, run_id, workspace_id) -> bool:
        return True


@pytest.fixture()
async def failing_executor() -> AsyncIterator[tuple[RunExecutor, _FailingProbeSession]]:
    session = _FailingProbeSession()
    executor = RunExecutor(
        session_factory=lambda: session,
        driver=_IdleDriver(),
        instance_id="resilience-test",
        idle_poll_interval=0.01,
    )
    await executor.start()
    try:
        yield executor, session
    finally:
        await executor.aclose()


async def test_queue_probe_failure_does_not_stop_loop(
    failing_executor: tuple[RunExecutor, _FailingProbeSession],
) -> None:
    """探测持续抛错时,循环必须仍然 running 并记录最近错误。"""
    executor, session = failing_executor

    await asyncio.sleep(0.15)

    assert executor.running is True
    assert executor.healthy is True
    assert isinstance(executor.last_error, RuntimeError)
    # 循环确实在重试,而不是抛一次就停:退避后仍在探测。
    assert session.probe_attempts >= 2


async def test_consecutive_failures_are_counted(
    failing_executor: tuple[RunExecutor, _FailingProbeSession],
) -> None:
    """连续失败次数可观测,供 readiness 判 degraded。"""
    executor, _ = failing_executor

    await asyncio.sleep(0.1)

    assert executor.consecutive_failures >= 1


async def test_aclose_succeeds_after_probe_failures(
    failing_executor: tuple[RunExecutor, _FailingProbeSession],
) -> None:
    """关闭路径不因循环内残留异常而失败。"""
    executor, _ = failing_executor

    await asyncio.sleep(0.05)
    await executor.aclose()

    assert executor.running is False


# ---------------------------------------------------------------------------
# 兜底扫描覆盖"等待回查":此前只有启动恢复会处理这类 Run
# ---------------------------------------------------------------------------


class _SingleRow:
    def __init__(self, row) -> None:
        self._row = row

    def first(self):
        return self._row


class _SequencedSession:
    """按调用顺序返回预设结果,用于观察扫描的状态优先级。"""

    def __init__(self, rows: list) -> None:
        self._rows = list(rows)
        self.queried = 0

    async def execute(self, stmt):
        self.queried += 1
        return _SingleRow(self._rows.pop(0))

    async def scalar(self, stmt):
        return None

    async def scalars(self, stmt):
        return _EmptyRows()

    async def close(self) -> None:
        return None

    def begin(self):
        return _NullContext()


async def test_scan_prefers_waiting_reconciliation() -> None:
    """等待回查的 Run 必须被兜底扫描捡到,否则它永久停在该状态。"""
    run_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    session = _SequencedSession([(run_id, workspace_id)])
    executor = RunExecutor(
        session_factory=lambda: session,
        driver=_IdleDriver(),
        instance_id="scan-test",
        idle_poll_interval=0.01,
    )

    assert await executor._scan_actionable() == (
        run_id,
        workspace_id,
        RunStatus.WAITING_RECONCILIATION,
    )
    assert session.queried == 1


class _RaisingReconcileDriver:
    async def advance(self, claim, *, answered=None):
        raise AssertionError("no run should be claimed")

    async def reconcile(self, run_id, workspace_id) -> bool:
        raise RuntimeError("reconcile source invalid")


async def test_reconcile_error_converges_run_to_failed() -> None:
    """回查抛错时 Run 必须收敛为终态,不能留在等待态空转。"""
    executor = RunExecutor(
        session_factory=_SequencedSession,
        driver=_RaisingReconcileDriver(),
        instance_id="reconcile-test",
        idle_poll_interval=0.01,
    )
    calls: list[dict] = []

    async def _record_failure(**kwargs) -> bool:
        calls.append(kwargs)
        return True

    executor._fail_run = _record_failure  # type: ignore[method-assign]

    resolved = await executor._reconcile_pending(uuid.uuid4(), uuid.uuid4())

    assert resolved is False
    assert calls and calls[0]["expected_status"] is RunStatus.WAITING_RECONCILIATION
    assert isinstance(executor.last_error, RuntimeError)
