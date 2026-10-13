"""Listing 查询能力的输入/输出模型(P1 Task 5;设计 §4.5)。

``get_listing(store_ref, sku)`` 只接收模型可见的店铺引用与 SKU;规范发布状态
只有 ``PUBLISHED`` / ``UNPUBLISHED`` / ``UNKNOWN``,平台审核、违规或资料缺失
以 ``issues`` 表达,不用发布状态承载。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    """frozen 值模型基类:结果对象不可变,只暴露白名单字段。"""

    model_config = ConfigDict(frozen=True)


class GetListingInput(_FrozenModel):
    """get_listing 输入:店铺引用 + SKU。"""

    store_ref: str = Field(description="用户当前消息明确给出的店铺名称或别名。")
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")


class ListingSnapshot(_FrozenModel):
    """单个 Listing 的读取快照;不含内部 UUID、成本价与平台凭证。"""

    store_ref: str
    sku: str
    title: str
    current_price: Decimal
    currency: str
    publication_state: Literal["PUBLISHED", "UNPUBLISHED", "UNKNOWN"]
    listing_version: int
    issues: tuple[str, ...] = ()


__all__ = ["GetListingInput", "ListingSnapshot"]
