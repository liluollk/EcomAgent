"""行为合同断言测试(协议 v3)。

这组测试证明的不是"断言写全了",而是"断言真的会失败":每一项都给出一个
最终状态完全正确、但过程做错的轨迹,合同判分必须把它判成不通过。否则补上
的字段只是装饰。
"""

from typing import Any

import pytest

from eval.dataset_loader import load_yaml
from eval.results import (
    CaseResult,
    FinalState,
    MessageCapture,
    ToolCallCapture,
)
from eval.schema import InventoryState, ListingState, Scenario
from eval.scorers.contract import ContractScorer


DATASET_DIR = "eval/datasets/product_operations"


def _scenario(**overrides: Any) -> Scenario:
    data: dict[str, Any] = {
        "id": "contract-case",
        "category": "product_operations",
        "risk_level": "medium",
        "initial_state": {
            "listings": [
                {
                    "store_id": "store-1",
                    "sku": "SKU-001",
                    "price": "128.00",
                    "currency": "CNY",
                    "status": "PUBLISHED",
                }
            ]
        },
        "workspace": {"name": "eval-workspace"},
        "actor": {"role": "owner"},
        "messages": [{"role": "user", "content": "把 SKU-001 改成 119.90 元"}],
        "expected_final_state": {"max_logical_writes": 1},
    }
    data.update(overrides)
    return Scenario.model_validate(data)


def _call(
    stable_key: str,
    *,
    ordinal: int = 1,
    sequence_no: int = 1,
    status: str = "SUCCEEDED",
    arguments: dict[str, Any] | None = None,
) -> ToolCallCapture:
    return ToolCallCapture(
        stable_key=stable_key,
        ordinal=ordinal,
        sequence_no=sequence_no,
        status=status,
        normalized_arguments=arguments,
    )


def _result(
    *,
    tool_calls: tuple[ToolCallCapture, ...] = (),
    messages: tuple[MessageCapture, ...] = (),
) -> CaseResult:
    return CaseResult(
        case_id="contract-case",
        repetition=0,
        status="COMPLETED",
        included_in_quality_score=True,
        fingerprint="fp",
        tool_calls=tool_calls,
        conversation_messages=messages,
        final_state=FinalState(
            listings=(
                ListingState(
                    store_id="store-1", sku="SKU-001", price=119.90, status="PUBLISHED"
                ),
            ),
            logical_writes=1,
        ),
    )


def _message(role: str, content: str, sequence_no: int) -> MessageCapture:
    return MessageCapture(
        run_id=None, sequence_no=sequence_no, role=role, content=content
    )


# ---- 入参 ---------------------------------------------------------------


def test_wrong_target_price_fails_though_state_is_right() -> None:
    """状态对、入参错:模型按错价写了一次(失败)再按对价写成 → 仍判不通过。"""
    scenario = _scenario(
        expected_tool_arguments=[
            {
                "tool": "set_listing_price",
                "field": "target_price",
                "value": "119.90",
                "scope": "all",
            }
        ]
    )
    calls = (
        _call(
            "set_listing_price",
            ordinal=1,
            status="FAILED",
            arguments={
                "store_ref": "store-1",
                "sku": "SKU-001",
                "target_price": "119.00",
            },
        ),
        _call(
            "set_listing_price",
            ordinal=2,
            sequence_no=2,
            arguments={
                "store_ref": "store-1",
                "sku": "SKU-001",
                "target_price": "119.90",
            },
        ),
    )
    score = ContractScorer(scenario).score(_result(tool_calls=calls))
    assert score.applicable is True
    assert score.passed is False
    assert any("argument_out_of_scope" in item for item in score.argument_violations)


def test_decimal_equivalent_price_passes() -> None:
    """``119.9`` 与 Gold 的 ``119.90`` 是同一个价格,不因写法判失败。"""
    scenario = _scenario(
        expected_tool_arguments=[
            {
                "tool": "set_listing_price",
                "field": "target_price",
                "value": "119.90",
            }
        ]
    )
    calls = (
        _call(
            "set_listing_price",
            arguments={
                "store_ref": "store-1",
                "sku": "SKU-001",
                "target_price": "119.9",
            },
        ),
    )
    assert ContractScorer(scenario).score(_result(tool_calls=calls)).passed is True


def test_multiset_matching_rejects_one_call_satisfying_two_expectations() -> None:
    """两条相反的 ``any`` 期望必须落在两次调用上,不能被一次调用同时满足。"""
    scenario = _scenario(
        expected_tool_arguments=[
            {"tool": "set_listing_price", "field": "target_price", "value": "75.00"},
            {"tool": "set_listing_price", "field": "target_price", "value": "82.00"},
        ]
    )
    calls = (
        _call(
            "set_listing_price",
            arguments={
                "store_ref": "store-1",
                "sku": "SKU-001",
                "target_price": "75.00",
            },
        ),
    )
    score = ContractScorer(scenario).score(_result(tool_calls=calls))
    assert score.passed is False
    assert any("call_count_short" in item for item in score.argument_violations)


def test_missing_call_is_reported_not_silently_skipped() -> None:
    """只读了没写:参数断言必须报"调用缺失",不能因为"没有调用"就当通过。"""
    scenario = _scenario(
        expected_tool_arguments=[
            {"tool": "set_listing_price", "field": "target_price", "value": "119.90"}
        ]
    )
    calls = (_call("get_listing", arguments={"store_ref": "store-1", "sku": "SKU-001"}),)
    score = ContractScorer(scenario).score(_result(tool_calls=calls))
    assert score.argument_violations == ("missing_call:set_listing_price",)


def test_reason_code_mismatch_is_caught() -> None:
    """库存数量对但原因码写成 MANUAL_CORRECTION:状态看不出来,合同看得见。"""
    scenario = _scenario(
        expected_final_state={
            "inventory_levels": [
                {
                    "store_id": "store-1",
                    "sku": "SKU-001",
                    "location_ref": "main",
                    "available_quantity": 25,
                }
            ],
            "max_logical_writes": 1,
        },
        expected_tool_arguments=[
            {
                "tool": "adjust_inventory_quantity",
                "field": "reason",
                "value": "RECEIPT",
                "scope": "all",
            }
        ],
    )
    result = CaseResult(
        case_id="contract-case",
        repetition=0,
        status="COMPLETED",
        included_in_quality_score=True,
        fingerprint="fp",
        tool_calls=(
            _call(
                "adjust_inventory_quantity",
                arguments={
                    "store_ref": "store-1",
                    "sku": "SKU-001",
                    "location_ref": "main",
                    "delta": 5,
                    "reason": "MANUAL_CORRECTION",
                },
            ),
        ),
        final_state=FinalState(
            inventory_levels=(
                InventoryState(
                    store_id="store-1",
                    sku="SKU-001",
                    location_ref="main",
                    available_quantity=25,
                ),
            ),
            logical_writes=1,
        ),
    )
    score = ContractScorer(scenario).score(result)
    assert score.passed is False
    expected_violation = (
        "argument_out_of_scope:adjust_inventory_quantity.reason=RECEIPT"
        "(实际:MANUAL_CORRECTION@SUCCEEDED)"
    )
    assert score.argument_violations == (expected_violation,)


def test_decimal_delta_matches_integer_expectation() -> None:
    """``delta`` 写 ``5`` 与 ``"5"`` 是同一个增量。"""
    scenario = _scenario(
        expected_tool_arguments=[
            {"tool": "adjust_inventory_quantity", "field": "delta", "value": "5"}
        ]
    )
    calls = (
        _call(
            "adjust_inventory_quantity",
            arguments={
                "store_ref": "store-1",
                "sku": "SKU-001",
                "delta": 5,
                "reason": "RECEIPT",
            },
        ),
    )
    assert ContractScorer(scenario).score(_result(tool_calls=calls)).passed is True


# ---- 禁止调用 -----------------------------------------------------------


def test_forbidden_write_attempt_fails_even_when_denied() -> None:
    """只读场景里"试了一下"就算违规,哪怕被拦下来、状态没动。"""
    scenario = _scenario(
        forbidden_tool_calls=["set_listing_price"],
        expected_final_state={"max_logical_writes": 0},
    )
    calls = (
        _call(
            "set_listing_price",
            status="DENIED",
            arguments={
                "store_ref": "store-1",
                "sku": "SKU-001",
                "target_price": "1.00",
            },
        ),
    )
    score = ContractScorer(scenario).score(_result(tool_calls=calls))
    assert score.passed is False
    assert score.forbidden_call_violations == ("forbidden_call:set_listing_price@DENIED",)


def test_read_only_scenario_without_attempt_passes() -> None:
    scenario = _scenario(
        forbidden_tool_calls=["set_listing_price"],
        expected_final_state={"max_logical_writes": 0},
    )
    calls = (_call("get_listing", arguments={"store_ref": "store-1", "sku": "SKU-001"}),)
    assert ContractScorer(scenario).score(_result(tool_calls=calls)).passed is True


# ---- 逐轮答复 -----------------------------------------------------------


def test_first_turn_answer_missing_is_caught() -> None:
    """第二轮答对、第一轮没答:逐轮判分必须能区分。"""
    scenario = _scenario(
        messages=[
            {"role": "user", "content": "先查 SKU-001 的现价"},
            {"role": "user", "content": "再改成 119.90 元"},
        ],
        turn_expectations=[
            {
                "after_message_index": 0,
                "expected_facts": [{"value": "128.00", "source": "tool_result"}],
            },
            {
                "after_message_index": 1,
                "expected_facts": [{"value": "119.90", "source": "final_state"}],
            },
        ],
    )
    messages = (
        _message("USER", "先查 SKU-001 的现价", 1),
        _message("TOOL", '{"price": "128.00"}', 2),
        _message("ASSISTANT", "正在查询。", 3),
        _message("USER", "再改成 119.90 元", 4),
        _message("ASSISTANT", "已改为 119.90 元。", 5),
    )
    score = ContractScorer(scenario).score(_result(messages=messages))
    assert score.passed is False
    assert score.turn_violations == ("turn1.omitted:128.00",)


def test_each_turn_checked_against_its_own_answer() -> None:
    """两轮都答对 → 通过;事实只出现在最后一轮不算两轮都对。"""
    scenario = _scenario(
        messages=[
            {"role": "user", "content": "先查 SKU-001 的现价"},
            {"role": "user", "content": "再改成 119.90 元"},
        ],
        turn_expectations=[
            {
                "after_message_index": 0,
                "expected_facts": [{"value": "128.00", "source": "tool_result"}],
            },
            {
                "after_message_index": 1,
                "expected_facts": [{"value": "119.90", "source": "final_state"}],
            },
        ],
    )
    messages = (
        _message("USER", "先查 SKU-001 的现价", 1),
        _message("TOOL", '{"price": "128.00"}', 2),
        _message("ASSISTANT", "现价 128.00 元。", 3),
        _message("USER", "再改成 119.90 元", 4),
        _message("ASSISTANT", "已改为 119.90 元。", 5),
    )
    score = ContractScorer(scenario).score(_result(messages=messages))
    assert score.turn_violations == ()
    assert score.passed is True


def test_turn_fact_without_tool_evidence_is_unsupported() -> None:
    """答复里出现的事实必须能在工具结果里找到,否则是凭空的数。"""
    scenario = _scenario(
        messages=[
            {"role": "user", "content": "先查 SKU-001 的现价"},
            {"role": "user", "content": "再改成 119.90 元"},
        ],
        turn_expectations=[
            {
                "after_message_index": 0,
                "expected_facts": [{"value": "128.00", "source": "tool_result"}],
            }
        ],
    )
    messages = (
        _message("USER", "先查 SKU-001 的现价", 1),
        _message("TOOL", '{"ok": true}', 2),
        _message("ASSISTANT", "现价 128.00 元。", 3),
        _message("USER", "再改成 119.90 元", 4),
        _message("ASSISTANT", "已改。", 5),
    )
    score = ContractScorer(scenario).score(_result(messages=messages))
    assert score.turn_violations == ("turn1.unsupported:128.00",)


# ---- 不适用 -------------------------------------------------------------


def test_scenario_without_contract_assertions_is_not_applicable() -> None:
    score = ContractScorer(_scenario()).score(_result())
    assert score.applicable is False
    assert score.passed is None
    assert score.checked_assertions == 0


# ---- 数据集覆盖(防止以后新增场景漏声明) ----------------------------

WRITE_TOOL = "set_listing_price"


@pytest.fixture(scope="module")
def core_scenarios() -> list[Scenario]:
    return load_yaml(f"{DATASET_DIR}/core.yaml")


def test_every_write_case_asserts_target_price(core_scenarios: list[Scenario]) -> None:
    """写场景必须断言目标价:状态对但入参错是最难发现的一类缺陷。"""
    missing = [
        scenario.id
        for scenario in core_scenarios
        if any(side.tool == WRITE_TOOL for side in scenario.allowed_side_effects)
        and not any(
            expectation.tool == WRITE_TOOL
            and expectation.field == "target_price"
            for expectation in scenario.expected_tool_arguments
        )
    ]
    assert missing == []


def test_every_read_only_case_forbids_write_attempts(
    core_scenarios: list[Scenario],
) -> None:
    """只读场景必须显式禁止写工具:``max_logical_writes=0`` 只挡落库,挡不住尝试。"""
    missing = [
        scenario.id
        for scenario in core_scenarios
        if scenario.category == "read_only_query"
        and WRITE_TOOL not in scenario.forbidden_tool_calls
    ]
    assert missing == []


def test_every_multiturn_case_checks_more_than_final_answer(
    core_scenarios: list[Scenario],
) -> None:
    """多轮场景必须逐轮判答复,不能只看最后一句。"""
    missing = [
        scenario.id
        for scenario in core_scenarios
        if scenario.category == "multi_turn" and not scenario.turn_expectations
    ]
    assert missing == []


def test_inventory_case_asserts_reason_and_delta(core_scenarios: list[Scenario]) -> None:
    """库存场景:数量正确不代表原因码正确,两个字段都要断言。"""
    inventory = [
        scenario
        for scenario in core_scenarios
        if scenario.category == "inventory_adjustment"
    ]
    assert inventory, "core 分区必须保留库存调整用例"
    fields = {
        expectation.field
        for scenario in inventory
        for expectation in scenario.expected_tool_arguments
    }
    assert {"reason", "delta"} <= fields
