"""任务完成 Scorer(计划 05 Task 3)。

判分只看权威最终状态(:class:`~eval.results.FinalState`,平台侧 Listing
与逻辑写次数)对照 :class:`~eval.schema.Scenario` 的
``expected_final_state``;模型文本宣称(assistant 消息、事件 payload 中
的任何自由文本)一律不参与判分——"文本说成功"不能替代平台权威状态。

``passed`` 语义 = 状态一致 **且** 逻辑写 ≤ ``max_logical_writes``:

- 状态一致:每条期望 Listing 按 ``(store_id, sku)`` 定位,仅比较期望中
  非 ``None`` 的断言字段(price/currency/status);期望未断言的字段与
  未断言的 Listing 不参与比较(期望是断言集合,不是全量快照);
- 权威最终状态缺失(``final_state is None``,如推进失败未捕获)时,存在
  任何状态断言即不一致——未完成/未捕获的运行永远不判成功。
"""

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult, FinalState
from eval.schema import ListingState, Scenario


class TaskCompletionScore(BaseModel):
    """任务完成维度的结构化分项。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    """状态一致且逻辑写不超过上限。"""

    run_completed: bool
    """Runtime Run 正常完成;中断/失败不能因状态碰巧相同计为完成。"""

    state_matches: bool
    """权威最终状态满足全部期望断言(价格/币种/状态)。"""

    logical_writes_within_limit: bool
    """权威逻辑写总次数 ≤ ``expected_final_state.max_logical_writes``。"""

    asserted_listings: int
    """期望中断言的 Listing 条数(分母语义,供报告使用)。"""

    mismatches: tuple[str, ...] = ()
    """不一致明细(``store/sku`` + 字段名或 ``:missing``),供归因。"""

    final_logical_writes: int | None
    """权威逻辑写总次数(未捕获为 None)。"""

    max_logical_writes: int
    """场景允许的最大逻辑写次数。"""


class TaskCompletionScorer:
    """绑定场景的任务完成 Scorer:权威状态 vs 期望断言,文本不参与。"""

    def __init__(self, scenario: Scenario) -> None:
        self._expected = scenario.expected_final_state

    def score(self, result: CaseResult) -> TaskCompletionScore:
        mismatches: list[str] = []
        actual = _listings_by_key(result.final_state)
        for expected in self._expected.listings:
            found = actual.get((expected.store_id, expected.sku))
            if found is None:
                mismatches.append(f"{expected.store_id}/{expected.sku}:missing")
                continue
            if expected.price is not None and found.price != expected.price:
                mismatches.append(f"{expected.store_id}/{expected.sku}.price")
            if expected.currency is not None and found.currency != expected.currency:
                mismatches.append(f"{expected.store_id}/{expected.sku}.currency")
            if expected.status is not None and found.status != expected.status:
                mismatches.append(f"{expected.store_id}/{expected.sku}.status")

        final_writes = result.final_state.logical_writes if result.final_state else None
        logical_writes = final_writes if final_writes is not None else 0
        within_limit = logical_writes <= self._expected.max_logical_writes
        return TaskCompletionScore(
            passed=(result.status == "COMPLETED")
            and result.final_state is not None
            and (not mismatches)
            and within_limit,
            run_completed=result.status == "COMPLETED",
            state_matches=not mismatches,
            logical_writes_within_limit=within_limit,
            asserted_listings=len(self._expected.listings),
            mismatches=tuple(mismatches),
            final_logical_writes=final_writes,
            max_logical_writes=self._expected.max_logical_writes,
        )


def _listings_by_key(final_state: FinalState | None) -> dict[tuple[str, str], ListingState]:
    """权威最终状态按 ``(store_id, sku)`` 键控(仅供定位期望 Listing)。"""
    if final_state is None:
        return {}
    return {
        (listing.store_id, listing.sku): listing for listing in final_state.listings
    }


__all__ = ["TaskCompletionScore", "TaskCompletionScorer"]
