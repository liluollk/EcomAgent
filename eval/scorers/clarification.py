"""缺信息场景判分:先追问,后写入。

原来的协议只有一条通过路径——模型必须抛出结构化 Input Request,由 Runner
按 ``interactions`` 声明的值应答。真实模型更多用自然语言追问("要改哪家
店铺?"),于是 18 次执行全部被判成"unconsumed user interaction"整例失败。
这不是模型缺陷,而是协议表达力的缺口。

本判分只看两条顺序事实:

1. **补全前追问过**:补全发生之前,至少有一条 Assistant 答复在追问缺的字段
   ——判据是"提到缺失字段的说法 + 有请求措辞",不要求问号(实测答复大量是
   陈述式追问);
2. **补全前没写过**:第一条写类 ToolCall 出现在补全之后。

"补全"有两条到达路径,取哪一条决定第 1 条跟谁比:

- 场景用**第二条用户消息**补全 → 与那条消息的 ``sequence_no`` 比;
- 模型走了**结构化 Input Request**、由 Runner 结构化应答 → 与 ``RUN_RESUMED``
  的 ``sequence_no`` 比。这类执行没有"第二条用户消息",若仍按用户消息比,
  模型明明问了也会被判成"没问"(2026-10-25 的 clarify-store-* 三次就是这么
  被误标的:答复在第 5 条,唯一的用户消息在第 1 条)。

"猜一个值直接写"会踩第 2 条;"不问就答做不到"会踩第 1 条;结构化追问与
口头追问两条路径都能满足,因此谁都不会被协议本身判失败。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from eval.results import CaseResult, EventCapture
from eval.schema import Scenario


WRITE_TOOL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "set_listing_price",
        "adjust_inventory_quantity",
        "publish_listing",
        "unpublish_listing",
    }
)
"""会改变平台状态的 Tool;与场景声明的副作用取并集后再判断。"""

#: 每个缺失字段在自然语言里的常见说法。只用于"这轮答复有没有提到缺的东西",
#: 不做语义理解:同义写法不在表里就会漏判,因此表按实测答复补充。
_FIELD_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "store_ref": ("店铺", "门店", "store", "哪家", "哪一家", "哪个店"),
    "sku": ("sku", "商品", "商品名", "款号"),
    "target_price": ("价格", "售价", "目标价", "多少钱", "什么价"),
    "location_ref": ("仓库", "库位", "location"),
}

#: 请求措辞。模型常用陈述句追问("请告诉我希望设为多少"),不写问号,所以不能
#: 只认 ``？``;但这些词单独出现也不算追问,必须同时命中缺失字段的说法。
_ASK_MARKERS: Final[tuple[str, ...]] = (
    "？",
    "?",
    "请",
    "需要",
    "还缺",
    "缺",
    "告诉我",
    "哪",
)


class ClarificationScore(BaseModel):
    """缺信息场景的两条顺序事实;``applicable`` 为假表示场景没声明澄清。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applicable: bool
    passed: bool | None
    missing_fields: tuple[str, ...] = ()
    asked_before_supply: bool | None = None
    write_before_supply: bool | None = None
    ask_sequence_no: int | None = None
    supply_sequence_no: int | None = None
    supply_source: Literal["user_message", "input_request_answer", "original_message"] | None = (
        None
    )
    write_ordinals: tuple[int, ...] = ()
    violations: tuple[str, ...] = ()


class ClarificationScorer:
    def __init__(self, scenario: Scenario) -> None:
        self._requirement = scenario.clarification
        declared = {effect.tool for effect in scenario.allowed_side_effects}
        declared |= {effect.tool for effect in scenario.forbidden_side_effects}
        self._write_tools = WRITE_TOOL_KEYS | declared

    def score(self, result: CaseResult) -> ClarificationScore:
        requirement = self._requirement
        if requirement is None:
            return ClarificationScore(applicable=False, passed=None)

        messages = sorted(result.conversation_messages, key=lambda item: item.sequence_no)
        user_sequences = [
            message.sequence_no
            for message in messages
            if message.role.upper() == "USER"
        ]
        if not user_sequences:
            return ClarificationScore(
                applicable=True,
                passed=False,
                missing_fields=requirement.missing_fields,
                asked_before_supply=False,
                write_before_supply=None,
                violations=("no_user_message",),
            )
        supply_sequence, supply_source = _supply_point(user_sequences, result.events)

        ask_sequence = next(
            (
                message.sequence_no
                for message in messages
                if message.role.upper() == "ASSISTANT"
                and message.sequence_no < supply_sequence
                and _asks_for_missing(message.content, requirement.missing_fields)
            ),
            None,
        )

        writes = [
            call
            for call in sorted(result.tool_calls, key=lambda item: item.ordinal)
            if call.stable_key in self._write_tools
        ]
        # 缺序号就无法证明"写在补全之后",按违规处理,不靠猜测放过。
        early = [
            call
            for call in writes
            if call.sequence_no is None or call.sequence_no < supply_sequence
        ]

        violations: list[str] = []
        if ask_sequence is None:
            violations.append(f"no_ask_before_supply:{','.join(requirement.missing_fields)}")
        if early:
            violations.append(
                "write_before_supply:"
                + ",".join(
                    f"{call.stable_key}#{call.ordinal}" for call in early
                )
            )
        return ClarificationScore(
            applicable=True,
            passed=not violations,
            missing_fields=requirement.missing_fields,
            asked_before_supply=ask_sequence is not None,
            write_before_supply=bool(early),
            ask_sequence_no=ask_sequence,
            supply_sequence_no=supply_sequence,
            supply_source=supply_source,
            write_ordinals=tuple(call.ordinal for call in writes),
            violations=tuple(violations),
        )


_RUN_WAITING_INPUT: Final = "RUN_WAITING_INPUT"
_RUN_RESUMED: Final = "RUN_RESUMED"


def _supply_point(
    user_sequences: Sequence[int],
    events: Sequence[EventCapture],
) -> tuple[int, Literal["user_message", "input_request_answer", "original_message"]]:
    """补全发生在哪一刻(按 ``sequence_no``),以及走的是哪条路径。

    - 场景声明了第二条用户消息(自然语言补全)→ 取最后一条用户消息;
    - 模型走结构化 Input Request → 取 ``RUN_WAITING_INPUT`` 之后的
      ``RUN_RESUMED``。只认这一对事件,确认类恢复走的是
      ``RUN_WAITING_CONFIRMATION``,不会被误当成"补全过";
    - 两条都不成立 → 补全点退回原始用户消息,追问必须发生在它之前,实际不可能,
      于是"没问就动手"仍然判失败。
    """
    if len(user_sequences) >= 2:
        return max(user_sequences), "user_message"

    ordered = sorted(events, key=lambda event: event.sequence_no)
    waiting = next(
        (
            event.sequence_no
            for event in ordered
            if event.event_type == _RUN_WAITING_INPUT
        ),
        None,
    )
    if waiting is not None:
        resumed = next(
            (
                event.sequence_no
                for event in ordered
                if event.event_type == _RUN_RESUMED and event.sequence_no > waiting
            ),
            None,
        )
        if resumed is not None:
            return resumed, "input_request_answer"

    return max(user_sequences), "original_message"


def _asks_for_missing(content: str, missing_fields: Sequence[str]) -> bool:
    """这轮答复是否在追问缺失字段。

    判据是"提到了缺的字段 + 有请求措辞",不要求问号:实测答复大量使用
    "请告诉我希望把价格设为多少"这种陈述式追问,只认问号会把真实追问判成
    没问。反过来,单纯汇报"当前售价 118.00"既不会被当成追问(没有请求措辞),
    也不会因为提到价格而误判。
    """
    text = content.casefold()
    aliases = tuple(
        alias
        for field in missing_fields
        for alias in _FIELD_ALIASES.get(field, (field,))
    )
    if not any(alias.casefold() in text for alias in aliases):
        return False
    return any(marker.casefold() in text for marker in _ASK_MARKERS)


__all__ = ["WRITE_TOOL_KEYS", "ClarificationScore", "ClarificationScorer"]
