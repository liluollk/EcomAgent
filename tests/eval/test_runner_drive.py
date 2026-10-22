"""评测驱动语义:停在 QUEUED 的 Run 要再推一轮,不是直接判"卡住"。

执行器把推进未完成的 Run 退回 QUEUED 等下一轮,生产环境由后台兜底循环
捞起来;评测用的同步门面不跑后台循环,驱动方必须自己再推一次。否则一次
可恢复的退回就变成"stalled in non-terminal status QUEUED",真正的原因
(哪个工具、哪个错误码)全部丢失。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from eval.runner import EvalRunner, _DriveState
from eval.schema import Scenario
from runtime.interfaces.state import RunStatus


class _StubApplication:
    """只实现驱动方用到的 ``resume``;记录被推了几次。"""

    def __init__(self, views: list[Any]) -> None:
        self._views = views
        self.resumed: list[uuid.UUID] = []

    async def resume(self, run_id: uuid.UUID) -> Any:
        self.resumed.append(run_id)
        return self._views.pop(0) if self._views else _view(RunStatus.QUEUED, run_id)


def _view(status: RunStatus, run_id: uuid.UUID) -> Any:
    return SimpleNamespace(
        status=status,
        run_id=run_id,
        pending_input_id=None,
        pending_confirmation_id=None,
    )


async def _drive(
    application: Any, status: RunStatus
) -> tuple[Any, int, uuid.UUID]:
    run_id = uuid.uuid4()
    runner = cast("EvalRunner", object())
    case = cast("Scenario", SimpleNamespace(id="drive-case"))
    view, interaction_index = await EvalRunner._drive_current_run(
        runner,
        application,
        cast("Any", None),
        case,
        _DriveState(),
        _view(status, run_id),
        0,
    )
    return view, interaction_index, run_id


async def test_queued_run_is_driven_again_until_it_reaches_a_boundary() -> None:
    """QUEUED → resume 一次 → COMPLETED:驱动方把 Run 推到终态。"""
    run_id = uuid.uuid4()
    application = _StubApplication([_view(RunStatus.COMPLETED, run_id)])

    view, interaction_index, run_id = await _drive(application, RunStatus.QUEUED)

    assert view.status is RunStatus.COMPLETED
    assert interaction_index == 0
    assert application.resumed == [run_id]


async def test_repeated_queued_states_exhaust_the_drive_budget() -> None:
    """一直退回 QUEUED 时按步数预算收口,不会无限重推。"""
    application = _StubApplication([_view(RunStatus.QUEUED, uuid.uuid4())] * 64)

    with pytest.raises(RuntimeError, match="exceeded the drive step budget"):
        await _drive(application, RunStatus.QUEUED)


async def test_other_non_terminal_status_still_reports_a_stall() -> None:
    """非 QUEUED 的中间态仍按"卡住"报错:不能对未知状态瞎推。"""
    application = _StubApplication([])

    with pytest.raises(RuntimeError, match="stalled in non-terminal status"):
        await _drive(application, RunStatus.RUNNING)
    assert application.resumed == []
