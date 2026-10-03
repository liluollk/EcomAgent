"""价格 Policy 的值对象与注入配置(计划 03 Task 4;架构文档 §5、§6)。

全部为不可变值对象:决策继承 Runtime ``PolicyDecision`` 并补充
Policy/Listing 版本与目标价;配置(最低允许价、单次变动硬阈值、币种、
PolicyVersion)是结构化注入的 Workspace 权威配置,不从 Prompt、Memory
或 WeKnora 文档解析。阶段 3 Schema 暂无 PolicyVersion 表,先以
``PricePolicyConfig.policy_version`` 表达;后续引入该表后由调用方加载
替换,决策结构不变。
"""

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator

from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import PolicyDecision


class PricePolicyDecision(PolicyDecision):
    """价格 Policy 决策;允许时携带目标价,拒绝时携带确定性 reason_code。"""

    policy_version: str
    listing_version: int | None = None
    expected_new_price: Decimal | None = None


class PricePolicyConfig(BaseModel):
    """价格业务配置:结构化注入,不来自 Prompt/Memory(架构文档 §6 末条)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str
    currency: str
    min_allowed_price: Decimal
    max_price_change: Decimal

    @model_validator(mode="after")
    def validate_bounds(self) -> "PricePolicyConfig":
        if self.min_allowed_price < 0:
            raise ValueError("min_allowed_price must be non-negative")
        if self.max_price_change <= 0:
            raise ValueError("max_price_change must be positive")
        return self


class PriceWriteRequest(BaseModel):
    """``update_price`` 写请求值对象;全部字段为结构化事实,无自由文本证据。

    ``listing_id``/``sku`` 二选一:模型可见输入为 ``store_id + sku``,
    服务端补全后可直接携带 ``listing_id``(架构文档 §4.4)。
    ``expected_version`` 为确认时快照的 Listing Version,供乐观并发校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: uuid.UUID
    store_id: uuid.UUID
    target_price: Decimal
    expected_version: int
    actor: ActorContext
    sku: str | None = None
    listing_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_listing_reference(self) -> "PriceWriteRequest":
        if self.sku is None and self.listing_id is None:
            raise ValueError("sku or listing_id is required")
        return self


__all__ = [
    "PricePolicyConfig",
    "PricePolicyDecision",
    "PriceWriteRequest",
]
