"""Pricing 能力的输入/输出模型与领域错误码(P1 Task 6;设计 §4.3、§6)。

模型可见参数只使用 ``store_ref``(店铺名称或别名)与 ``sku``;内部
``store_id`` / ``listing_id`` / 平台凭证与 Adapter key 由服务端解析,不出现在
输入 Schema。价格比较只覆盖**已发布** Listing,并由确定性代码给出最低价、
最高价与价差;存在多币种时不比较、直接以 ``CURRENCY_MISMATCH`` 拒绝,绝不
由模型自行换算。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


CURRENCY_MISMATCH: Final = "CURRENCY_MISMATCH"
"""同一 SKU 的已发布 Listing 存在多个币种,禁止直接比较。"""

NO_PUBLISHED_LISTING: Final = "NO_PUBLISHED_LISTING"
"""授权店铺内该 SKU 没有可比较的已发布 Listing。"""


class _FrozenModel(BaseModel):
    """frozen 值模型基类:结果对象不可变,只暴露白名单字段。"""

    model_config = ConfigDict(frozen=True)


class CompareListingPricesInput(_FrozenModel):
    """compare_listing_prices 输入:SKU 与可选店铺引用集合。"""

    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")
    store_refs: tuple[str, ...] = Field(
        default=(),
        description=(
            "用户当前消息明确给出的店铺名称或别名;未限定时留空表示当前"
            "授权范围内的全部店铺。"
        ),
    )


class ListingPriceRow(_FrozenModel):
    """单个已发布 Listing 的价格行(模型可见:店铺引用 + 价格 + 币种)。"""

    store_ref: str
    price: Decimal
    currency: str


class PriceComparisonSummary(_FrozenModel):
    """确定性价格比较结果;全部行同币种,价差 = 最高价 - 最低价。"""

    sku: str
    currency: str
    rows: tuple[ListingPriceRow, ...]
    lowest_price: Decimal
    highest_price: Decimal
    price_spread: Decimal


class SetListingPriceInput(_FrozenModel):
    """set_listing_price 输入:店铺引用 + SKU + 目标价。"""

    store_ref: str = Field(description="用户当前消息明确给出的店铺名称或别名。")
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")
    target_price: Decimal = Field(description="最终目标售价;服务端校验合法性与业务规则。")


class SetListingPriceResult(_FrozenModel):
    """set_listing_price 输出回执;UNKNOWN 时无观测 payload。"""

    operation_id: str
    policy_version: str
    store_ref: str
    sku: str
    currency: str
    target_price: Decimal
    listing_version: int | None = None
    external_ref: str | None = None


__all__ = [
    "CURRENCY_MISMATCH",
    "NO_PUBLISHED_LISTING",
    "CompareListingPricesInput",
    "ListingPriceRow",
    "PriceComparisonSummary",
    "SetListingPriceInput",
    "SetListingPriceResult",
]
