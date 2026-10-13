"""Inventory 业务策略(P1 Task 7;设计 §5.2、§6)。

设计 §5.2 要求 Policy 组件提供**预检**与**执行校验**两个入口,并共享同一份
版本化配置,避免复制规则。库存规则固定覆盖:

1. 调整原因属于受控枚举;
2. ``delta != 0``(no-op 调整没有业务意义,确定性拒绝);
3. 履约位置存在且属于该店铺(不泄漏跨店/跨 Workspace 引用);
4. (位置, 商品) 的库存记录存在;
5. 调整后数量非负;
6. ``expected_version`` 与当前库存版本一致(乐观并发)。

任何失败返回确定性 ``reason_code`` 的 denied 决策,不抛异常;DENY 由调用方
(Handler / PreToolUse 管线)处理。规则所需一切来自结构化输入与 PostgreSQL
Session,不读取 Prompt、Memory 或任何文档。
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.inventory.models import InventoryAdjustmentReason
from agents.product_operations.persistence.models import InventoryLevel
from agents.product_operations.persistence.repository import ProductRepository
from runtime.execution.pretool.decisions import PolicyDecision


INVALID_REASON: Final = "INVALID_REASON"
ZERO_DELTA: Final = "ZERO_DELTA"
LOCATION_NOT_IN_STORE: Final = "LOCATION_NOT_IN_STORE"
INVENTORY_NOT_FOUND: Final = "INVENTORY_NOT_FOUND"
NEGATIVE_RESULT: Final = "NEGATIVE_RESULT"
INVENTORY_VERSION_CONFLICT: Final = "INVENTORY_VERSION_CONFLICT"

_DENY_MESSAGES: Final[dict[str, str]] = {
    INVALID_REASON: "adjustment reason is not a controlled value",
    ZERO_DELTA: "delta must be non-zero",
    LOCATION_NOT_IN_STORE: "location does not belong to the store",
    INVENTORY_NOT_FOUND: "inventory level does not exist for the location and product",
    NEGATIVE_RESULT: "adjusted quantity would be negative",
    INVENTORY_VERSION_CONFLICT: "inventory version has changed since it was read",
}

_VALID_REASONS: Final[frozenset[str]] = frozenset(
    reason.value for reason in InventoryAdjustmentReason
)


class InventoryPolicyConfig(BaseModel):
    """库存业务配置:结构化注入,不来自 Prompt/Memory(架构文档 §6 末条)。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str


class InventoryPolicyDecision(PolicyDecision):
    """库存 Policy 决策;允许时携带库存版本,拒绝时携带确定性 reason_code。"""

    policy_version: str
    inventory_version: int | None = None


class InventoryPolicy:
    """库存业务策略:只读 PostgreSQL,返回 InventoryPolicyDecision 值对象。"""

    def __init__(self, session: AsyncSession, config: InventoryPolicyConfig) -> None:
        self._session = session
        self._config = config
        self._repository = ProductRepository(session)

    @property
    def policy_version(self) -> str:
        return self._config.policy_version

    async def evaluate(
        self,
        *,
        workspace_id: uuid.UUID,
        store_id: uuid.UUID,
        location_id: uuid.UUID,
        product_id: uuid.UUID,
        delta: int,
        reason: str,
        expected_version: int,
    ) -> InventoryPolicyDecision:
        """按固定次序校验调整请求;全部通过返回 allowed 决策。"""
        if reason not in _VALID_REASONS:
            return self._deny(INVALID_REASON)
        if delta == 0:
            return self._deny(ZERO_DELTA)

        location = await self._repository.get_fulfillment_location(workspace_id, location_id)
        if location is None or location.store_id != store_id:
            return self._deny(LOCATION_NOT_IN_STORE)

        level = await self._repository.get_inventory_level(workspace_id, location_id, product_id)
        if level is None:
            return self._deny(INVENTORY_NOT_FOUND)

        if level.available_quantity + delta < 0:
            return self._deny(NEGATIVE_RESULT, level=level)
        if expected_version != level.version:
            return self._deny(INVENTORY_VERSION_CONFLICT, level=level)
        return InventoryPolicyDecision(
            allowed=True,
            policy_version=self._config.policy_version,
            inventory_version=level.version,
        )

    def _deny(
        self, reason_code: str, *, level: InventoryLevel | None = None
    ) -> InventoryPolicyDecision:
        return InventoryPolicyDecision(
            allowed=False,
            reason_code=reason_code,
            safe_message=_DENY_MESSAGES[reason_code],
            policy_version=self._config.policy_version,
            inventory_version=level.version if level is not None else None,
        )


__all__ = [
    "INVALID_REASON",
    "INVENTORY_NOT_FOUND",
    "INVENTORY_VERSION_CONFLICT",
    "LOCATION_NOT_IN_STORE",
    "NEGATIVE_RESULT",
    "ZERO_DELTA",
    "InventoryPolicy",
    "InventoryPolicyConfig",
    "InventoryPolicyDecision",
]
