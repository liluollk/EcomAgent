"""故障恢复 Scorer(计划 05 Task 3)。

从事件序列提取 UNKNOWN 后的回查路径并对照场景 ``allowed_paths``
(``expected_reconcile_path``,如 ``("UNKNOWN", "APPLIED")``),统计:

- ``recovery_success``:全部 UNKNOWN 均被正确处理 **且** 提取路径全部命中
  允许路径(未发生 UNKNOWN 时空真);
- ``unknown_handled_correctly``:UNKNOWN 后经回查(OPERATION_RECONCILED)
  得出结论、且未发生盲目重执行的 UNKNOWN 次数;
- ``duplicate_logical_writes``:确认落库(APPLIED)的 Operation 总逻辑写
  次数超出场景 ``expected_final_state.max_logical_writes`` 的部分——同一
  逻辑变更的重复提交(跨 Operation 多次提交或单 Operation 内多次写)均
  计入,与恢复套件(Task 6)的 ``actual_duplicate_writes`` 同语义。

路径提取规则(事件来源,均按 ``sequence_no`` 排序):

- UNKNOWN 判定:``TOOL_RESULT_RECORDED`` 且 ``payload["outcome"]=="UNKNOWN"``;
- 路径起点记 ``UNKNOWN``;其后同 ``tool_call_id`` 的每条
  ``OPERATION_RECONCILED`` 的 ``payload["result"]``(``APPLIED``/
  ``NOT_APPLIED``/``UNKNOWN``)依次追加为路径步;
- 正确处理 = 至少一条回查结论事件,且该 ``tool_call_id`` 的
  ``TOOL_RESULT_RECORDED`` 不超过 UNKNOWN 那一条(回查后再执行即盲目重试)。
"""

from typing import Final

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult, EventCapture
from eval.schema import Scenario


_RECONCILE_RESULTS: Final[frozenset[str]] = frozenset({"APPLIED", "NOT_APPLIED", "UNKNOWN"})

_TOOL_RESULT_RECORDED: Final[str] = "TOOL_RESULT_RECORDED"
_RUN_WAITING_RECONCILIATION: Final[str] = "RUN_WAITING_RECONCILIATION"
_OPERATION_RECONCILED: Final[str] = "OPERATION_RECONCILED"


class RecoveryScore(BaseModel):
    """故障恢复维度的结构化分项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recovery_applicable: bool
    """场景声明了恢复路径或轨迹中至少出现一次 UNKNOWN。"""

    recovery_success: bool | None
    """恢复适用时的结果;不适用返回 None,避免普通用例空真通过。"""

    unknown_encounters: int
    """出现 UNKNOWN 结论的写操作次数。"""

    unknown_handled_correctly: int
    """UNKNOWN 后经回查得出结论且未盲目重执行的次数。"""

    duplicate_logical_writes: int
    """确认落库操作中超出一次逻辑写的重复写次数。"""

    reconcile_paths: tuple[tuple[str, ...], ...] = ()
    """提取的回查路径(每条以 ``UNKNOWN`` 起点)。"""

    paths_match_allowed: bool
    """提取路径全部命中场景 ``allowed_paths``(场景未声明路径时空真)。"""


class RecoveryScorer:
    """绑定场景的恢复 Scorer:回查路径 / UNKNOWN 处理 / 重复逻辑写。"""

    def __init__(self, scenario: Scenario) -> None:
        self._allowed_paths = frozenset(
            path.steps for path in scenario.allowed_paths if path.steps
        )
        self._max_logical_writes = scenario.expected_final_state.max_logical_writes

    def score(self, result: CaseResult) -> RecoveryScore:
        events = sorted(result.events, key=lambda event: event.sequence_no)
        unknown_ids = _unknown_tool_call_ids(events)
        paths: list[tuple[str, ...]] = []
        handled = 0
        for tool_call_id in unknown_ids:
            path, handled_correctly = _trace_unknown(events, tool_call_id)
            paths.append(path)
            if handled_correctly:
                handled += 1
        recovery_applicable = bool(self._allowed_paths or unknown_ids)
        paths_match = (
            bool(paths) and all(path in self._allowed_paths for path in paths)
            if self._allowed_paths
            else True
        )
        return RecoveryScore(
            recovery_applicable=recovery_applicable,
            recovery_success=(
                handled == len(unknown_ids) and paths_match
                if recovery_applicable
                else None
            ),
            unknown_encounters=len(unknown_ids),
            unknown_handled_correctly=handled,
            duplicate_logical_writes=_duplicate_logical_writes(
                result, max_logical_writes=self._max_logical_writes
            ),
            reconcile_paths=tuple(paths),
            paths_match_allowed=paths_match,
        )


def _unknown_tool_call_ids(events: list[EventCapture]) -> tuple[str, ...]:
    """按事件序提取出现 UNKNOWN 结论的 tool_call_id(去重保序)。"""
    found: list[str] = []
    for event in events:
        tool_call_id = event.payload.get("tool_call_id")
        if not isinstance(tool_call_id, str):
            continue
        is_unknown = (
            event.event_type == _TOOL_RESULT_RECORDED
            and event.payload.get("outcome") == "UNKNOWN"
        ) or event.event_type == _RUN_WAITING_RECONCILIATION
        if is_unknown and tool_call_id not in found:
            found.append(tool_call_id)
    return tuple(found)


def _trace_unknown(events: list[EventCapture], tool_call_id: str) -> tuple[tuple[str, ...], bool]:
    """单条 UNKNOWN 的回查路径与"是否正确处理"。"""
    path: list[str] = ["UNKNOWN"]
    reconciled = False
    tool_result_events = 0
    for event in events:
        if event.payload.get("tool_call_id") != tool_call_id:
            continue
        if event.event_type == _TOOL_RESULT_RECORDED:
            tool_result_events += 1
        elif event.event_type == _OPERATION_RECONCILED:
            conclusion = event.payload.get("result")
            if isinstance(conclusion, str) and conclusion in _RECONCILE_RESULTS:
                path.append(conclusion)
                reconciled = True
    # 正确处理:有回查结论,且除 UNKNOWN 本身外没有再执行的结果事件。
    handled_correctly = reconciled and tool_result_events <= 1
    return tuple(path), handled_correctly


def _duplicate_logical_writes(result: CaseResult, *, max_logical_writes: int) -> int:
    """确认落库(APPLIED)写超出场景允许最大逻辑写的部分(不猜测 UNKNOWN)。"""
    total_writes = sum(
        operation.logical_writes
        for operation in result.operations
        if operation.status == "APPLIED"
    )
    return max(0, total_writes - max_logical_writes)


__all__ = ["RecoveryScore", "RecoveryScorer"]
