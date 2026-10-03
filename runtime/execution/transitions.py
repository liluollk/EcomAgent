"""Run 显式状态迁移表与校验(计划 02 Task 3;架构文档 §4.1)。

``RUN_TRANSITIONS`` 逐字实现计划给出的迁移表:终态 ``COMPLETED``/
``FAILED``/``CANCELLED`` 的合法目标集合为空。非法迁移抛出
:class:`InvalidTransition`(携带 from/to 与可选 reason)。本模块为纯
逻辑,只依赖公共类型 ``runtime.interfaces.state``,不触达数据库。
"""

from runtime.interfaces.state import RunStatus


class InvalidTransition(RuntimeError):
    """非法 Run 状态迁移;``current``/``target`` 为迁移两端状态。"""

    def __init__(
        self,
        *,
        current: RunStatus,
        target: RunStatus,
        reason: str | None = None,
    ) -> None:
        self.current = current
        self.target = target
        self.reason = reason
        message = f"Illegal run transition: {current.value} -> {target.value}"
        if reason is not None:
            message = f"{message} ({reason})"
        super().__init__(message)


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({
        RunStatus.WAITING_INPUT,
        RunStatus.WAITING_CONFIRMATION,
        RunStatus.WAITING_RECONCILIATION,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }),
    RunStatus.WAITING_INPUT: frozenset({RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.WAITING_CONFIRMATION: frozenset({RunStatus.QUEUED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.WAITING_RECONCILIATION: frozenset({RunStatus.QUEUED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    """校验迁移合法性;非法时抛 :class:`InvalidTransition`(含 from/to 信息)。"""
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidTransition(current=current, target=target)


def legal_transitions(status: RunStatus) -> frozenset[RunStatus]:
    """返回该状态的合法目标集合(终态为空集)。"""
    return RUN_TRANSITIONS[status]
