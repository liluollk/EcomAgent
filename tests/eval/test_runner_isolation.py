"""Eval Runner 隔离与基础设施失败测试(计划 05 Task 2)。

覆盖计划原文两个测试(用例顺序无关、Provider 401 归基础设施失败)与
Step 3 语义的逐条落实:

- 每 case/repetition 独立 Workspace/Session/业务状态:两个 case 各自跑后
  互不可见对方数据(各自 authoritative price 独立落到各自 Listing);
- 完整捕获(AgentEvent/ToolCall/Operation/权威最终状态)进入 CaseResult;
- 任一 skipped case 使整轮协议失效(``protocol_valid=False``);
- Provider 401 / 连接失败 / 依赖不可用归 INFRASTRUCTURE_ERROR,不计入
  质量分母;
- 正常确定性 case 用注入的 ScriptedBackend 端到端跑通真实 Runtime
  (真库产生真实事件与权威状态,COMPLETED 且计入质量分母);
- 结果结构 frozen 且可 JSON 序列化。
"""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.worker.e2e import E2EScriptedBackend
from eval.config import EvalConfig
from eval.results import EvalRoundResult, by_case_id
from eval.runner import EvalRunner
from eval.schema import Scenario, protocol_fingerprint


async def test_case_order_does_not_change_results(
    eval_runner: EvalRunner, two_cases: list[Scenario]
) -> None:
    """计划原文用例:执行顺序不影响结果(按 case_id 键控且内容一致)。"""
    forward = await eval_runner.run(two_cases)
    reverse = await eval_runner.run(list(reversed(two_cases)))
    assert by_case_id(forward) == by_case_id(reverse)
    assert all(result.status == "COMPLETED" for result in forward.results)
    assert all(result.included_in_quality_score for result in forward.results)


async def test_provider_401_is_infrastructure_failure(
    eval_runner: EvalRunner, valid_case: Scenario, provider_401: Any
) -> None:
    """计划原文用例:Provider 401 是基础设施失败,不计入质量分母。"""
    result = await eval_runner.run_one(valid_case, backend=provider_401)
    assert result.status == "INFRASTRUCTURE_ERROR"
    assert result.included_in_quality_score is False
    assert result.fingerprint == protocol_fingerprint(valid_case, eval_runner.config)


async def test_connection_failure_is_infrastructure_error(
    eval_runner: EvalRunner, valid_case: Scenario, connection_error_backend: Any
) -> None:
    """连接失败(OSError 系)同样归 INFRASTRUCTURE_ERROR。"""
    result = await eval_runner.run_one(valid_case, backend=connection_error_backend)
    assert result.status == "INFRASTRUCTURE_ERROR"
    assert result.included_in_quality_score is False


async def test_completed_case_captures_full_trace(
    eval_runner: EvalRunner, valid_case: Scenario, eval_config: EvalConfig
) -> None:
    """确定性 case 端到端跑通真实 Runtime:事件/ToolCall/Operation/权威状态完整捕获。"""
    result = await eval_runner.run_one(valid_case)
    assert result.status == "COMPLETED"
    assert result.included_in_quality_score is True
    assert result.case_id == "price-001"
    assert result.repetition == 0
    assert result.fingerprint == protocol_fingerprint(valid_case, eval_config)
    assert result.run_id is not None

    # 事件流:ToolCall 提案 → 等输入 → 恢复 → 等确认 → 恢复 → 写结果 →
    # 完成(QUEUED→RUNNING 迁移发生在租约领取,不产生 AgentEvent)。
    event_types = [event.event_type for event in result.events]
    assert "TOOL_CALL_PROPOSED" in event_types
    assert "RUN_WAITING_INPUT" in event_types
    assert "RUN_WAITING_CONFIRMATION" in event_types
    assert "TOOL_RESULT_RECORDED" in event_types
    assert event_types[-1] == "RUN_COMPLETED"
    assert [event.sequence_no for event in result.events] == sorted(
        event.sequence_no for event in result.events
    )

    # ToolCall:update_price SUCCEEDED;Operation:平台写入记录存在。
    assert [(call.stable_key, call.status) for call in result.tool_calls] == [
        ("update_price", "SUCCEEDED")
    ]
    assert [operation.status for operation in result.operations] == ["APPLIED"]

    # 权威最终状态:平台侧价格 99.00,仅一次逻辑写。
    assert result.final_state is not None
    assert result.final_state.logical_writes == 1
    assert len(result.final_state.listings) == 1
    listing = result.final_state.listings[0]
    assert listing.store_id == "store-1"
    assert listing.sku == "SKU-001"
    assert listing.price == Decimal("99.00")


async def test_cases_run_in_isolated_workspaces(
    eval_runner: EvalRunner, two_cases: list[Scenario]
) -> None:
    """每 case 独立 Workspace/Session/业务状态:互不可见对方权威数据。"""
    forward = await eval_runner.run(two_cases)
    by_id = {result.case_id: result for result in forward.results}

    price_001, price_002 = by_id["price-001"], by_id["price-002"]
    assert price_001.workspace_id is not None
    assert price_002.workspace_id is not None
    assert price_001.workspace_id != price_002.workspace_id
    assert price_001.run_id is not None and price_001.run_id != price_002.run_id

    # 各自 authoritative price 独立:两个 case 都从 128.00 种子出发,
    # 若业务状态串扰,后跑的 case 会看到先跑 case 的结果价。
    assert price_001.final_state is not None
    assert price_002.final_state is not None
    assert price_001.final_state.listings[0].price == Decimal("99.00")
    assert price_002.final_state.listings[0].price == Decimal("120.00")
    assert price_001.final_state.logical_writes == 1
    assert price_002.final_state.logical_writes == 1
    # 痕迹互不串扰:各自事件与 ToolCall 只属于各自的 Run。
    assert price_001.tool_calls and price_002.tool_calls
    assert price_001.events and price_002.events


async def test_repetitions_are_isolated(
    engine: AsyncEngine, eval_config: EvalConfig, valid_case: Scenario
) -> None:
    """同一 case 的多次重复彼此独立(各自全新 Workspace 与业务状态)。"""
    runner = EvalRunner(
        engine,
        config=eval_config,
        backend_factory=lambda scenario: E2EScriptedBackend(),
        repetitions=2,
    )
    forward = await runner.run([valid_case])
    by_id = by_case_id(forward)
    assert len(by_id["price-001"]) == 2
    assert all(content.status == "COMPLETED" for content in by_id["price-001"])
    assert sorted(result.repetition for result in forward.results) == [0, 1]
    workspace_ids = {result.workspace_id for result in forward.results}
    assert None not in workspace_ids
    assert len(workspace_ids) == 2


async def test_skipped_case_invalidates_round_protocol(
    eval_runner: EvalRunner, two_cases: list[Scenario], second_case: Scenario
) -> None:
    """任一 skipped case 使整轮协议失效:protocol_valid=False。"""
    forward = await eval_runner.run(two_cases, skip_ids={second_case.id})
    assert forward.protocol_valid is False
    skipped = [result for result in forward.results if result.case_id == second_case.id]
    assert skipped
    assert all(result.status == "SKIPPED" for result in skipped)
    assert all(result.included_in_quality_score is False for result in skipped)

    complete_round = await eval_runner.run(two_cases)
    assert complete_round.protocol_valid is True


async def test_round_counts_reflect_statuses(
    eval_runner: EvalRunner, two_cases: list[Scenario]
) -> None:
    """轮次计数按状态真实汇总。"""
    forward = await eval_runner.run(two_cases)
    assert forward.completed == 2
    assert forward.infrastructure_errors == 0
    assert forward.skipped == 0
    assert forward.failed == 0
    assert forward.protocol_valid is True


async def test_results_are_frozen_and_json_serializable(
    eval_runner: EvalRunner, two_cases: list[Scenario]
) -> None:
    """CaseResult/EvalRoundResult 为 frozen 模型且可 JSON 序列化回读。"""
    forward = await eval_runner.run(two_cases)
    payload = json.dumps(forward.model_dump(mode="json"))
    restored = EvalRoundResult.model_validate_json(payload)
    assert restored == forward

    result = forward.results[0]
    with pytest.raises(ValidationError):
        result.status = "FAILED"  # type: ignore[misc]
