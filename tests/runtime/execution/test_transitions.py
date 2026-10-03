"""Run 状态迁移表测试(计划 02 Task 3;架构文档 §4.1)。"""

import pytest

from runtime.execution.transitions import (
    RUN_TRANSITIONS,
    InvalidTransition,
    ensure_run_transition,
    legal_transitions,
)
from runtime.interfaces.state import RunStatus


def test_cancelled_run_cannot_resume() -> None:
    """计划原文:终态 CANCELLED 不允许迁移回 QUEUED。"""
    with pytest.raises(InvalidTransition):
        ensure_run_transition(RunStatus.CANCELLED, RunStatus.QUEUED)


def test_transition_table_covers_every_status() -> None:
    """迁移表必须显式覆盖全部 RunStatus 成员,不留隐式默认。"""
    assert set(RUN_TRANSITIONS) == set(RunStatus)


def test_transition_table_matches_plan() -> None:
    """迁移表与计划 02 Task 3 / 架构文档 §4.1 给出的目标集合逐项一致。"""
    expected: dict[RunStatus, frozenset[RunStatus]] = {
        RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
        RunStatus.RUNNING: frozenset(
            {
                RunStatus.WAITING_INPUT,
                RunStatus.WAITING_CONFIRMATION,
                RunStatus.WAITING_RECONCILIATION,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }
        ),
        RunStatus.WAITING_INPUT: frozenset(
            {RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}
        ),
        RunStatus.WAITING_CONFIRMATION: frozenset(
            {RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}
        ),
        RunStatus.WAITING_RECONCILIATION: frozenset({RunStatus.QUEUED, RunStatus.FAILED}),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.FAILED: frozenset(),
        RunStatus.CANCELLED: frozenset(),
    }
    assert RUN_TRANSITIONS == expected


@pytest.mark.parametrize(
    "terminal_status",
    [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED],
)
def test_terminal_status_has_no_outgoing_transitions(terminal_status: RunStatus) -> None:
    """终态 COMPLETED/FAILED/CANCELLED 的合法目标集合为空。"""
    assert legal_transitions(terminal_status) == frozenset()


def test_full_transition_matrix() -> None:
    """全集测试:每个 (current, target) 组合要么合法,要么抛含 from/to 的 InvalidTransition。"""
    for current in RunStatus:
        for target in RunStatus:
            if target in RUN_TRANSITIONS[current]:
                ensure_run_transition(current, target)
            else:
                with pytest.raises(InvalidTransition) as exc_info:
                    ensure_run_transition(current, target)
                assert exc_info.value.current == current
                assert exc_info.value.target == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING),
        (RunStatus.RUNNING, RunStatus.WAITING_INPUT),
        (RunStatus.WAITING_INPUT, RunStatus.QUEUED),
        (RunStatus.RUNNING, RunStatus.WAITING_RECONCILIATION),
        (RunStatus.WAITING_RECONCILIATION, RunStatus.QUEUED),
        (RunStatus.RUNNING, RunStatus.CANCELLED),
    ],
)
def test_legal_transition_passes(current: RunStatus, target: RunStatus) -> None:
    """代表性合法迁移(QUEUED→RUNNING、RUNNING→WAITING_INPUT、WAITING_INPUT→QUEUED 等)通过。"""
    ensure_run_transition(current, target)
