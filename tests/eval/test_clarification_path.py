"""缺信息场景:口头追问必须能通过,猜值直接写必须判失败。

v3 的 Clarification 分区 18 次执行全部失败,原因不在模型,而在协议只认
"结构化 Input Request"这一条通过路径。这里分两段验证:

- 端到端(真实 Runtime):第一轮只回一句问句、第二轮补上店铺后才写 ——
  补充消息必须真的被投递,任务才能做完;
- 单元:同一份场景,把轨迹换成"不问就挑一家店写"或"问了但没写",判分必须
  分别抓住这两类错误。状态相同、只有顺序不同,才说明断言不是在装饰。
"""

from __future__ import annotations

from typing import Any

import pytest

from eval.results import (
    CaseResult,
    EventCapture,
    FinalState,
    MessageCapture,
    ToolCallCapture,
)
from eval.runner import EvalRunner
from eval.schema import ListingState, Scenario
from eval.scorers.clarification import ClarificationScorer
from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)


CLARIFY_SCENARIO: dict[str, Any] = {
    "id": "clarify-e2e",
    "category": "incomplete_info",
    "risk_level": "medium",
    "initial_state": {
        "listings": [
            {
                "store_id": "store-alpha",
                "sku": "SKU-3003",
                "price": "96.00",
                "currency": "CNY",
                "status": "PUBLISHED",
            },
            {
                "store_id": "store-beta",
                "sku": "SKU-3003",
                "price": "108.00",
                "currency": "CNY",
                "status": "PUBLISHED",
            },
        ]
    },
    "workspace": {"name": "eval-workspace"},
    "actor": {
        "role": "owner",
        "permissions": ["product.read", "product.price.write"],
        "store_scope_mode": "ALL",
    },
    "messages": [
        {"role": "user", "content": "把 SKU-3003 改到 88.00 元。"},
        {"role": "user", "content": "改 store-alpha 的。"},
    ],
    "interactions": [
        {"kind": "input", "message_index": 1, "values": {"store_ref": "store-alpha"}}
    ],
    "clarification": {"missing_fields": ["store_ref"]},
    "expected_final_state": {
        "listings": [
            {"store_id": "store-alpha", "sku": "SKU-3003", "price": "88.00"},
            {"store_id": "store-beta", "sku": "SKU-3003", "price": "108.00"},
        ],
        "max_logical_writes": 1,
    },
    "allowed_side_effects": [
        {"tool": "set_listing_price", "target": "store-alpha/SKU-3003"}
    ],
}


def _asking_backend() -> Any:
    """第一轮只问不写,用户补上店铺后才发起改价。"""

    class AskingBackend:
        def capabilities(self) -> AgentBackendCapabilities:
            return AgentBackendCapabilities(
                capability_keys=frozenset({"streaming", "tool_calling"})
            )

        async def stream(self, request: BackendRequest, cancellation: CancellationToken):
            del cancellation
            if any(message.role == "tool" for message in request.messages):
                yield TextDelta(text_delta="已把 store-alpha 的 SKU-3003 改成 88.00 元。")
                yield BackendCompleted(finish_reason="stop")
                return
            last_user = next(
                (
                    message.content
                    for message in reversed(request.messages)
                    if message.role == "user"
                ),
                "",
            )
            if "store-alpha" not in last_user:
                yield TextDelta(
                    text_delta="SKU-3003 在两家店铺在售，要改哪一家？请指定店铺。"
                )
                yield BackendCompleted(finish_reason="stop")
                return
            yield ToolCallCompleted(
                call_id="clarify-price",
                name="set_listing_price",
                arguments={
                    "store_ref": "store-alpha",
                    "sku": "SKU-3003",
                    "target_price": "88.00",
                },
            )
            yield BackendCompleted(finish_reason="tool_calls")

    return AskingBackend()


def _message(sequence_no: int, role: str, content: str) -> MessageCapture:
    return MessageCapture(
        run_id=None, sequence_no=sequence_no, role=role, content=content
    )


def _result(
    messages: tuple[MessageCapture, ...],
    calls: tuple[ToolCallCapture, ...],
) -> CaseResult:
    return CaseResult(
        case_id="clarify-e2e",
        repetition=0,
        status="COMPLETED",
        included_in_quality_score=True,
        fingerprint="fp",
        conversation_messages=messages,
        tool_calls=calls,
        final_state=FinalState(
            listings=(
                ListingState(store_id="store-alpha", sku="SKU-3003", price="88.00"),
                ListingState(store_id="store-beta", sku="SKU-3003", price="108.00"),
            ),
            logical_writes=1,
        ),
    )


async def test_natural_language_question_is_accepted(eval_runner: EvalRunner) -> None:
    """口头追问不再被判成"未消费交互":补全消息照常投递,任务能做完。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result: CaseResult = await eval_runner.run_one(case, backend=_asking_backend())

    assert result.status == "COMPLETED", result.error
    prices = {
        (item.store_id, item.sku): str(item.price) for item in result.final_state.listings
    }
    assert prices[("store-alpha", "SKU-3003")] == "88.00"
    assert prices[("store-beta", "SKU-3003")] == "108.00"
    # 补充消息确实是第二轮投递的:会话里能看到两条用户输入。
    assert sum(1 for item in result.conversation_messages if item.role == "USER") == 2

    score = ClarificationScorer(case).score(result)
    assert score.applicable is True
    assert score.asked_before_supply is True
    assert score.write_before_supply is False
    assert score.passed is True


def test_guessing_before_asking_is_rejected() -> None:
    """不问就挑一家店写下去:最终状态碰巧对了也要判失败。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result = _result(
        messages=(
            _message(1, "USER", "把 SKU-3003 改到 88.00 元。"),
            _message(2, "ASSISTANT", "已把 store-alpha 的 SKU-3003 改成 88.00 元。"),
            _message(3, "USER", "改 store-alpha 的。"),
        ),
        calls=(
            ToolCallCapture(
                stable_key="set_listing_price",
                ordinal=1,
                sequence_no=2,
                status="SUCCEEDED",
            ),
        ),
    )

    score = ClarificationScorer(case).score(result)
    assert score.passed is False
    assert score.asked_before_supply is False
    assert score.write_before_supply is True
    assert any(item.startswith("write_before_supply") for item in score.violations)


def test_answer_without_question_is_rejected() -> None:
    """既没问也没写(直接说做不到)不算澄清:顺序对了,但缺了追问。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result = _result(
        messages=(
            _message(1, "USER", "把 SKU-3003 改到 88.00 元。"),
            _message(2, "ASSISTANT", "无法完成，需要更多信息。"),
            _message(3, "USER", "改 store-alpha 的。"),
        ),
        calls=(),
    )

    score = ClarificationScorer(case).score(result)
    assert score.passed is False
    assert score.asked_before_supply is False
    assert score.write_before_supply is False
    assert score.violations == ("no_ask_before_supply:store_ref",)


def test_scenario_without_clarification_still_requires_declared_input() -> None:
    """没声明澄清的场景不许走"口头追问"豁免:确认类交互更不能被绕过。"""
    payload = dict(CLARIFY_SCENARIO)
    payload.pop("clarification")
    case = Scenario.model_validate(payload)
    assert EvalRunner._natural_language_clarification(case, case.interactions) is False

    confirmation = dict(CLARIFY_SCENARIO)
    confirmation.pop("clarification")
    confirmation["interactions"] = [
        {"kind": "confirmation", "message_index": 1, "decision": "accept"}
    ]
    confirmed = Scenario.model_validate(confirmation)
    assert (
        EvalRunner._natural_language_clarification(confirmed, confirmed.interactions)
        is False
    )


def _price_scenario() -> Scenario:
    payload: dict[str, Any] = dict(CLARIFY_SCENARIO)
    payload["id"] = "clarify-price-unit"
    payload["messages"] = [
        {"role": "user", "content": "修改 store-eta 的 SKU-7001 售价。"},
        {"role": "user", "content": "把 store-eta 的 SKU-7001 改成 112.50 元。"},
    ]
    payload["interactions"] = [
        {"kind": "input", "message_index": 1, "values": {"target_price": "112.50"}}
    ]
    payload["clarification"] = {"missing_fields": ["target_price"]}
    payload["expected_tool_arguments"] = [
        {
            "tool": "set_listing_price",
            "field": "target_price",
            "value": "112.50",
            "scope": "all",
        }
    ]
    return Scenario.model_validate(payload)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # 陈述式追问:没有问号,但提到了缺的目标价并明确请用户提供。
        ("当前售价 118.00。要改价还缺一个关键信息：目标价格。请告诉我希望设为多少。", True),
        # 只汇报现状,没请用户补充:不算追问。
        ("已读取当前刊登：store-eta / SKU-7001，当前售价 118.00 CNY，状态 PUBLISHED。", False),
    ],
)
def test_statement_style_ask_is_recognized(answer: str, expected: bool) -> None:
    """追问判据不能只认问号:模型常用陈述句追问。"""
    case = _price_scenario()
    result = _result(
        messages=(
            _message(1, "USER", "修改 store-eta 的 SKU-7001 售价。"),
            _message(2, "ASSISTANT", answer),
            _message(3, "USER", "把 store-eta 的 SKU-7001 改成 112.50 元。"),
            _message(4, "ASSISTANT", "已改价为 112.50。"),
        ),
        calls=(
            ToolCallCapture(
                stable_key="set_listing_price",
                ordinal=1,
                sequence_no=4,
                status="SUCCEEDED",
            ),
        ),
    )

    score = ClarificationScorer(case).score(result)
    assert score.asked_before_supply is expected
    assert score.write_before_supply is False
    assert score.passed is expected


# ---- 结构化补全路径:补全点是 RUN_RESUMED,不是"第二条用户消息" ----------
#
# 场景声明的是"第二条用户消息补全"或"结构化 Input Request 应答"两种之一。
# 走结构化时,会话里只有一条 USER,拿它当补全点会让"已经问过"的答复落到
# 补全点之后,于是被记成"没问"(2026-10-25 的 clarify-store-* 三次)。


def _event(sequence_no: int, event_type: str) -> EventCapture:
    return EventCapture(sequence_no=sequence_no, event_type=event_type, payload={})


def _structured_result(
    messages: tuple[MessageCapture, ...],
    events: tuple[EventCapture, ...],
    calls: tuple[ToolCallCapture, ...] = (),
) -> CaseResult:
    return CaseResult(
        case_id="clarify-e2e",
        repetition=0,
        status="COMPLETED",
        included_in_quality_score=True,
        fingerprint="fp",
        conversation_messages=messages,
        events=events,
        tool_calls=calls,
    )


def _input_cycle(*, waiting: int, resumed: int) -> tuple[EventCapture, ...]:
    """一次结构化补全:等用户输入 → 收到应答后恢复。"""
    return (
        _event(waiting, "RUN_WAITING_INPUT"),
        _event(resumed, "RUN_RESUMED"),
    )


def test_structured_supply_uses_resume_as_the_supply_point() -> None:
    """只投递了一条用户消息时,补全点必须落在 RUN_RESUMED 上。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result = _structured_result(
        messages=(
            _message(1, "USER", "把 SKU-3003 改到 88.00 元。"),
            _message(2, "ASSISTANT", "SKU-3003 在两家店铺在售，要改哪一家？请指定店铺。"),
            _message(3, "ASSISTANT", "已把 store-alpha 的 SKU-3003 改成 88.00 元。"),
        ),
        events=_input_cycle(waiting=4, resumed=5),
        calls=(
            ToolCallCapture(
                stable_key="set_listing_price",
                ordinal=1,
                sequence_no=6,
                status="SUCCEEDED",
            ),
        ),
    )

    score = ClarificationScorer(case).score(result)
    assert score.supply_source == "input_request_answer"
    assert score.supply_sequence_no == 5
    assert score.asked_before_supply is True
    assert score.write_before_supply is False
    assert score.passed is True


def test_structured_supply_without_a_question_is_still_rejected() -> None:
    """结构化了不等于问过了:恢复前后都没有追问,照样判失败。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result = _structured_result(
        messages=(
            _message(1, "USER", "把 SKU-3003 改到 88.00 元。"),
            _message(2, "ASSISTANT", "已读取两家店铺的刊登信息。"),
            _message(3, "ASSISTANT", "已把 store-alpha 的 SKU-3003 改成 88.00 元。"),
        ),
        events=_input_cycle(waiting=4, resumed=5),
        calls=(
            ToolCallCapture(
                stable_key="set_listing_price",
                ordinal=1,
                sequence_no=6,
                status="SUCCEEDED",
            ),
        ),
    )

    score = ClarificationScorer(case).score(result)
    assert score.asked_before_supply is False
    assert score.passed is False
    assert score.violations == ("no_ask_before_supply:store_ref",)


def test_question_after_the_answer_does_not_count() -> None:
    """拿到值之后才补一句"要改哪家":顺序不对,不算补全前追问。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result = _structured_result(
        messages=(
            _message(1, "USER", "把 SKU-3003 改到 88.00 元。"),
            _message(6, "ASSISTANT", "请再确认一下要改哪一家店铺？"),
        ),
        events=_input_cycle(waiting=3, resumed=4),
    )

    score = ClarificationScorer(case).score(result)
    assert score.asked_before_supply is False
    assert score.passed is False


def test_confirmation_resume_is_not_treated_as_supply() -> None:
    """确认类恢复前面没有 RUN_WAITING_INPUT:不能当成"补全过"。"""
    case = Scenario.model_validate(CLARIFY_SCENARIO)
    result = _structured_result(
        messages=(
            _message(1, "USER", "把 SKU-3003 改到 88.00 元。"),
            _message(2, "ASSISTANT", "确认要在 store-alpha 改价吗？"),
        ),
        events=(_event(3, "RUN_RESUMED"),),
    )

    score = ClarificationScorer(case).score(result)
    assert score.supply_source == "original_message"
    assert score.ask_sequence_no is None
    assert score.passed is False
