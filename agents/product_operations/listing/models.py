"""Listing 能力的输入/输出模型(P1 Task 5 查询;Task 8 增加发布状态写入)。

``get_listing(store_ref, sku)`` 只接收模型可见的店铺引用与 SKU;规范发布状态
只有 ``PUBLISHED`` / ``UNPUBLISHED`` / ``UNKNOWN``,平台审核、违规或资料缺失
以 ``issues`` 表达,不用发布状态承载。

``publish_listing(store_ref, sku)`` 与 ``unpublish_listing(store_ref, sku,
reason?)`` 同样只接收模型可见引用;内部 ``store_id`` / ``listing_id``、Listing
版本与幂等凭据由服务端补全。取消发布的原因不是自由文本——否则无法审计与回放——
而是受 :class:`UnpublishReason` 约束,并要求"库存为零不等价于取消发布"(设计
§4.5):刊登状态与库存是两个独立事实,取消发布不得顺手清零库存。
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field


INVALID_UNPUBLISH_REASON: Final = "INVALID_UNPUBLISH_REASON"
"""取消发布原因不属于受控枚举时的规则码(作 ``VALIDATION_FAILED`` 的 ``rule_code``)。"""


class UnpublishReason(str, enum.Enum):
    """取消发布原因受控枚举(P1 固定三个取值,不可由模型自由发挥)。

    设计 §4.5 明确"库存为零不等价于取消发布",因此这里不提供"缺货"一类的原因:
    缺货是库存事实,不是刊登决策;需要停售时由运营显式选择原因。
    """

    MANUAL = "MANUAL"
    """运营手动下架。"""

    COMPLIANCE = "COMPLIANCE"
    """合规、违规或平台风控要求下架。"""

    SEASONAL_END = "SEASONAL_END"
    """季节性或活动周期结束。"""


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


class PublishListingInput(_FrozenModel):
    """publish_listing 输入:店铺引用 + SKU(目标状态由 Tool 固定为 PUBLISHED)。"""

    store_ref: str = Field(description="用户当前消息明确给出的店铺名称或别名。")
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")


class UnpublishListingInput(_FrozenModel):
    """unpublish_listing 输入:店铺引用 + SKU + 可选受控原因。"""

    store_ref: str = Field(description="用户当前消息明确给出的店铺名称或别名。")
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")
    reason: UnpublishReason | None = Field(
        default=None,
        description=(
            "取消发布的受控原因;省略时不记录原因。原因不是自由文本,"
            "必须取受控枚举值。"
        ),
    )


class ListingPublicationResult(_FrozenModel):
    """发布状态变更回执;UNKNOWN 时无观测 payload。

    ``publication_state`` 只在 ``APPLIED`` 时出现,取值即服务端固定的目标状态
    (``PUBLISHED`` 或 ``UNPUBLISHED``)——未经确认的 ``UNKNOWN`` 不允许在这里
    被表达成某个状态。

    ``changed`` 是平台侧"本次调用是否真的改变了状态":目标状态与当前状态相同时
    为 ``False``,表示这是一次幂等 no-op,模型不必再重试同一请求。
    """

    operation_id: str
    policy_version: str
    store_ref: str
    sku: str
    publication_state: Literal["PUBLISHED", "UNPUBLISHED"]
    reason: str | None = None
    listing_version: int | None = None
    external_ref: str | None = None
    changed: bool | None = None


__all__ = [
    "INVALID_UNPUBLISH_REASON",
    "GetListingInput",
    "ListingPublicationResult",
    "ListingSnapshot",
    "PublishListingInput",
    "UnpublishListingInput",
    "UnpublishReason",
]
