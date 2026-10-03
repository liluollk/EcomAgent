"""商品域参数的确定性归一化(计划 03 Task 3;架构文档 §5)。

只做确定性变换,不做任何模型猜测:

- 字段白名单:白名单之外的字段(模型自报 ``source``/``confidence`` 等)
  直接丢弃,Provenance 阶段因此天然看不见它们;
- 字符串值 ``trim``;
- 金额字符串 → 两位小数的 ``Decimal``;非法金额保留 trim 后的原值,交由
  上层(Provenance/校验)判定,而不是静默修正或抛异常。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Final


# 写 Tool 的关键参数(架构文档 §5);同时是归一化的字段白名单。
KEY_ARGUMENT_FIELDS: Final[tuple[str, ...]] = ("store_id", "sku", "target_price")

AMOUNT_QUANTUM: Final = Decimal("0.01")

# 金额字符串白名单:正的十进制数,至多两位小数;不接受符号、指数与千分位。
_AMOUNT_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{1,12}(\.\d{1,2})?\Z")


def normalize_label(text: str) -> str:
    """证据匹配用的确定性文本归一:大小写折叠 + 去除全部空白字符。"""
    return "".join(text.split()).casefold()


def normalize_amount(value: Any) -> Decimal | None:
    """合法金额 → 两位小数 ``Decimal``;其余返回 ``None``。

    合法:非负、有限、至多两位小数的字符串/整数/``Decimal``。字符串一律
    先 ``trim``;超过两位小数、负数、非法文本都不是合法金额(不静默舍入)。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or _AMOUNT_PATTERN.fullmatch(text) is None:
            return None
        amount = Decimal(text)
    else:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    try:
        quantized = amount.quantize(AMOUNT_QUANTUM)
    except InvalidOperation:
        return None
    if quantized != amount:
        return None
    return quantized


def normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """按字段白名单归一化模型 ToolCall 参数:白名单、``trim``、金额 Decimal 化。

    返回的新 dict 只含白名单字段;``target_price`` 合法时变为两位小数
    ``Decimal``,非法时保留 trim 后的原值等待校验判定。
    """
    normalized: dict[str, Any] = {}
    for key in KEY_ARGUMENT_FIELDS:
        if key not in arguments:
            continue
        value = arguments[key]
        if isinstance(value, str):
            value = value.strip()
        if key == "target_price":
            amount = normalize_amount(value)
            if amount is not None:
                value = amount
        normalized[key] = value
    return normalized


__all__ = [
    "AMOUNT_QUANTUM",
    "KEY_ARGUMENT_FIELDS",
    "normalize_amount",
    "normalize_arguments",
    "normalize_label",
]
