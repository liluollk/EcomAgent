"""Inventory 能力的输入/输出模型与领域错误(P1 Task 7;设计 §4.4、§6)。

模型可见参数只使用 ``store_ref``(店铺名称或别名)、``sku`` 与可选
``location_ref``(履约位置名称或别名);内部 ``store_id`` / ``location_id`` /
``product_id`` 与平台凭证由服务端解析,不出现在输入 Schema。库存调整只表达
**增量**(``delta``)而非绝对值,并按受控枚举给出调整原因——原因不可由自由文本
承担,否则无法审计与回放。
"""

from __future__ import annotations

import enum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from agents.product_operations.shared.errors import (
    AMBIGUOUS_LOCATION,
    ProductOperationError,
)


INVALID_DELTA: Final = "INVALID_DELTA"
"""``delta`` 不是整数或为零时的规则码(作 ``VALIDATION_FAILED`` 的 ``rule_code``)。"""

INVALID_REASON: Final = "INVALID_REASON"
"""调整原因不属于受控枚举时的规则码。"""


class InventoryAdjustmentReason(str, enum.Enum):
    """库存调整原因受控枚举(固定四个取值,不可由模型自由发挥)。"""

    MANUAL_CORRECTION = "MANUAL_CORRECTION"
    RECEIPT = "RECEIPT"
    DAMAGE = "DAMAGE"
    RETURN = "RETURN"


class AmbiguousLocationError(ProductOperationError):
    """位置引用匹配到多个位置,无法确定性解析,需要用户补充输入。

    继承 :class:`ProductOperationError` 以复用规范错误码通道:``code`` 固定为
    ``AMBIGUOUS_LOCATION``。调用方既可捕获具体类型,也可按 ``code`` 归口。
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(AMBIGUOUS_LOCATION, detail)


class _FrozenModel(BaseModel):
    """frozen 值模型基类:结果对象不可变,只暴露白名单字段。"""

    model_config = ConfigDict(frozen=True)


class GetInventoryLevelInput(_FrozenModel):
    """get_inventory_level 输入:店铺引用 + SKU + 可选位置引用。"""

    store_ref: str = Field(description="用户当前消息明确给出的店铺名称或别名。")
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")
    location_ref: str | None = Field(
        default=None,
        description=(
            "用户当前消息明确给出的履约位置名称或别名;未给出时,若该店铺仅有"
            "唯一启用位置则自动解析,存在多个位置则要求用户补充。"
        ),
    )


class InventoryLevelResult(_FrozenModel):
    """get_inventory_level 输出:位置解析后的权威库存快照。"""

    store_ref: str
    sku: str
    location_ref: str
    available_quantity: int
    inventory_version: int


class AdjustInventoryQuantityInput(_FrozenModel):
    """adjust_inventory_quantity 输入:店铺/位置引用 + SKU + 增量 + 受控原因。"""

    store_ref: str = Field(description="用户当前消息明确给出的店铺名称或别名。")
    sku: str = Field(description="用户当前消息明确给出的 SKU 原值。")
    location_ref: str | None = Field(
        default=None,
        description=(
            "用户当前消息明确给出的履约位置名称或别名;未给出时,若该店铺仅有"
            "唯一启用位置则自动解析,存在多个位置则要求用户补充。"
        ),
    )
    delta: int = Field(description="库存增量(可为负);服务端校验调整后数量非负。")
    reason: InventoryAdjustmentReason = Field(description="库存调整原因,受控枚举。")


class AdjustInventoryQuantityResult(_FrozenModel):
    """adjust_inventory_quantity 输出回执;UNKNOWN 时无观测 payload。"""

    operation_id: str
    policy_version: str
    store_ref: str
    sku: str
    location_ref: str
    delta: int
    available_quantity: int | None = None
    inventory_version: int | None = None
    external_ref: str | None = None


__all__ = [
    "INVALID_DELTA",
    "INVALID_REASON",
    "AdjustInventoryQuantityInput",
    "AdjustInventoryQuantityResult",
    "AmbiguousLocationError",
    "GetInventoryLevelInput",
    "InventoryAdjustmentReason",
    "InventoryLevelResult",
]
