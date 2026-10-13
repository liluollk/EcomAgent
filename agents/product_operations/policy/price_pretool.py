"""改价写 Tool 的 PreToolUse 业务策略(计划 04 Task 3、Task 7;架构文档 §5、§6)。

- ``update_price`` 走现有 :class:`PricePolicy`(最低允许价、单次变动硬阈值、
  乐观并发);其余 Tool 放行;
- 首次评估(进入确认前)把 Listing 版本作为**资源版本**放进
  ``PolicyDecision.resource_versions``,由 Runtime 写进
  ``ConfirmationSpec`` 并持久化到 ConfirmationRequest;
- 确认后恢复重评时,从**已接受 ConfirmationRequest** 读回原始资源版本并与当前
  ``Listing.version`` 比较,不一致即以 ``LISTING_VERSION_CONFLICT`` 拒绝——
  该比对跨进程重启依然有效,不再依赖进程内 ``_version_bindings`` 字典。

本模块只通过请求级 Session 与 Actor 调用现有 PricePolicy,不把 Repository
移入 Runtime;依赖方向 policy → tools(取 stable_key),不含反向依赖。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.policy.models import (
    PricePolicyConfig,
    PricePolicyDecision,
    PriceWriteRequest,
)
from agents.product_operations.policy.price_policy import (
    LISTING_NOT_FOUND as POLICY_LISTING_NOT_FOUND,
)
from agents.product_operations.policy.price_policy import (
    LISTING_VERSION_CONFLICT as POLICY_LISTING_VERSION_CONFLICT,
)
from agents.product_operations.policy.price_policy import PricePolicy
from agents.product_operations.tools.update_price import STABLE_KEY_UPDATE_PRICE
from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import PolicyDecision
from runtime.persistence.base import ConfirmationRequestStatus
from runtime.persistence.models.interaction import ConfirmationRequest


def listing_resource_key(listing_id: uuid.UUID) -> str:
    """确认资源版本的键:``listing:<id>``(与架构文档 §6 一致)。"""
    return f"listing:{listing_id}"


async def resolve_listing(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    store_id: uuid.UUID,
    sku: str,
) -> Listing | None:
    """按 (workspace, store, sku) 读取 Listing(供 Policy 与权威回读)。"""
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


class PricePreToolPolicy:
    """``update_price`` 的业务策略实现(实现 Runtime ``BusinessPolicy`` 协议)。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        config: PricePolicyConfig,
        actor: ActorContext,
        tool_call_id: uuid.UUID,
    ) -> None:
        self._session = session
        self._config = config
        self._actor = actor
        self._tool_call_id = tool_call_id

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        del resources  # 资源范围已在 Access Control 阶段校验。
        if stable_key != STABLE_KEY_UPDATE_PRICE:
            return PolicyDecision(allowed=True)
        decision_or_none = self._parse(arguments)
        if decision_or_none is not None:
            return decision_or_none
        store_id, sku, price = self._arguments_of(arguments)
        listing = await resolve_listing(
            self._session, self._actor.workspace_id, store_id, sku
        )
        if listing is None:
            return self._deny(
                POLICY_LISTING_NOT_FOUND,
                "listing does not exist in this workspace",
                expected_new_price=price,
            )
        conflict = await self._confirmation_version_conflict(
            listing.id, listing.version
        )
        if conflict:
            return self._deny(
                POLICY_LISTING_VERSION_CONFLICT,
                "listing version has changed since confirmation",
                expected_new_price=price,
            )
        request = PriceWriteRequest(
            workspace_id=self._actor.workspace_id,
            store_id=store_id,
            target_price=price,
            expected_version=listing.version,
            actor=self._actor,
            sku=sku,
        )
        decision = await PricePolicy(self._session, self._config).evaluate(request)
        if not decision.allowed:
            return decision
        return decision.model_copy(
            update={
                "resource_versions": {
                    listing_resource_key(listing.id): listing.version
                }
            }
        )

    async def _confirmation_version_conflict(
        self, listing_id: uuid.UUID, current_version: int
    ) -> bool:
        """已接受确认记录的 Listing 版本与当前版本不一致即为冲突。"""
        accepted = (
            await self._session.scalars(
                select(ConfirmationRequest.resource_versions)
                .where(
                    ConfirmationRequest.workspace_id == self._actor.workspace_id,
                    ConfirmationRequest.tool_call_id == self._tool_call_id,
                    ConfirmationRequest.status == ConfirmationRequestStatus.ACCEPTED,
                )
                .order_by(
                    ConfirmationRequest.created_at.desc(),
                    ConfirmationRequest.id.desc(),
                )
                .limit(1)
            )
        ).one_or_none()
        if not accepted:
            return False
        stored = dict(accepted).get(listing_resource_key(listing_id))
        if stored is None:
            return False
        return int(stored) != current_version

    def _arguments_of(
        self, arguments: Mapping[str, Any]
    ) -> tuple[uuid.UUID, str, Decimal]:
        """前置条件:``_parse`` 已保证参数形状合法。"""
        return (
            uuid.UUID(str(arguments["store_id"])),
            str(arguments["sku"]),
            _cast_decimal(arguments["target_price"]),
        )

    def _parse(self, arguments: Mapping[str, Any]) -> PolicyDecision | None:
        """参数形状校验;非法返回确定性拒绝,合法返回 None。"""
        store_id = arguments.get("store_id")
        sku = arguments.get("sku")
        price = arguments.get("target_price")
        valid_shape = (
            isinstance(store_id, str)
            and isinstance(sku, str)
            and isinstance(price, Decimal)
        )
        if valid_shape:
            try:
                uuid.UUID(store_id)
            except ValueError:
                valid_shape = False
        if valid_shape:
            return None
        return self._deny(
            POLICY_LISTING_NOT_FOUND,
            "price update target cannot be resolved from validated arguments",
            expected_new_price=None,
        )

    def _deny(
        self,
        reason_code: str,
        safe_message: str,
        *,
        expected_new_price: Decimal | None,
    ) -> PricePolicyDecision:
        return PricePolicyDecision(
            allowed=False,
            reason_code=reason_code,
            safe_message=safe_message,
            policy_version=self._config.policy_version,
            listing_version=None,
            expected_new_price=expected_new_price,
        )


def _cast_decimal(value: Any) -> Decimal:
    """前置条件:调用方已确认 ``isinstance(value, Decimal)``。"""
    result: Decimal = value
    return result


__all__ = ["PricePreToolPolicy", "listing_resource_key", "resolve_listing"]
