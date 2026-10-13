"""Pricing 业务策略(P1 Task 6;设计 §5.2、§6)。

设计 §5.2 要求 Policy 组件提供**预检**与**执行校验**两个入口,并共享同一份
版本化配置,避免复制规则。本模块因此只做两件事:

- 用 :class:`~agents.product_operations.policy.price_policy.PricePolicy` 承载
  唯一一份价格规则(最低允许价、单次变动硬阈值、币种、乐观并发);
- 把预检与执行校验都委托给同一份规则,并复用其稳定 reason_code。

价格规则的字符串码值与实现仍是既有 :mod:`agents.product_operations.policy`
模块的单一事实源:本模块不复制规则,只按能力边界重新暴露入口(P1 的过渡
形态;规则整体搬迁发生在后续任务)。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.policy.models import (
    PricePolicyConfig,
    PricePolicyDecision,
    PriceWriteRequest,
)
from agents.product_operations.policy.price_policy import (
    LISTING_NOT_FOUND,
    LISTING_VERSION_CONFLICT,
    PricePolicy,
)
from runtime.access.actor import ActorContext


async def resolve_listing_for_sku(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    store_id: uuid.UUID,
    sku: str,
) -> Listing | None:
    """按 ``(workspace, store, sku)`` 读取 Listing 的确定性查询。"""
    return (
        await session.scalars(
            select(Listing)
            .join(Product, Listing.product_id == Product.id)
            .where(
                Listing.workspace_id == workspace_id,
                Listing.store_id == store_id,
                Product.workspace_id == workspace_id,
                Product.sku == sku,
            )
        )
    ).one_or_none()


class PricingPolicy:
    """价格能力的业务策略;预检与执行校验共享同一版本化配置。"""

    def __init__(self, session: AsyncSession, config: PricePolicyConfig) -> None:
        self._session = session
        self._config = config

    @property
    def policy_version(self) -> str:
        return self._config.policy_version

    async def preflight(
        self,
        *,
        actor: ActorContext,
        store_id: uuid.UUID,
        sku: str,
        target_price: Decimal,
    ) -> PricePolicyDecision:
        """PreToolUse 预检:解析当前 Listing 版本后走执行校验的同一套规则。

        Listing 不存在时返回确定性 ``LISTING_NOT_FOUND`` 拒绝,不猜测版本。
        """
        listing = await resolve_listing_for_sku(
            self._session, actor.workspace_id, store_id, sku
        )
        if listing is None:
            return PricePolicyDecision(
                allowed=False,
                reason_code=LISTING_NOT_FOUND,
                safe_message="listing does not exist in this workspace",
                policy_version=self._config.policy_version,
                expected_new_price=target_price,
            )
        return await self.validate_execution(
            actor=actor,
            store_id=store_id,
            sku=sku,
            target_price=target_price,
            expected_version=listing.version,
        )

    async def validate_execution(
        self,
        *,
        actor: ActorContext,
        store_id: uuid.UUID,
        sku: str,
        target_price: Decimal,
        expected_version: int,
    ) -> PricePolicyDecision:
        """执行校验:在构造可信写命令前复核业务不变量与乐观并发。"""
        request = PriceWriteRequest(
            workspace_id=actor.workspace_id,
            store_id=store_id,
            target_price=target_price,
            expected_version=expected_version,
            actor=actor,
            sku=sku,
        )
        return await PricePolicy(self._session, self._config).evaluate(request)


__all__ = [
    "LISTING_NOT_FOUND",
    "LISTING_VERSION_CONFLICT",
    "PricePolicyConfig",
    "PricePolicyDecision",
    "PricingPolicy",
    "resolve_listing_for_sku",
]
