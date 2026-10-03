"""``update_price`` 写前价格 Policy(计划 03 Task 4;架构文档 §6)。

校验次序固定;任何失败返回确定性 reason_code 的 denied
``PricePolicyDecision``,不抛异常,DENY 由 PreToolUse 管线处理:

1. Listing 存在(按 ``listing_id`` 或 ``store_id + sku`` 解析)且状态可写;
2. 目标价 Decimal 合法性(正数、最多两位小数、不等于现价)与币种一致;
3. 不低于当前生效的最低允许价;
4. 无禁止改价的活动锁价;
5. 单次变动不超过硬阈值(绝对金额);
6. ``expected_version`` 与当前 Listing Version 一致(乐观并发)。

Policy 决策所需一切来自结构化输入与当前 PostgreSQL Session,不读取
Prompt、Memory 或 WeKnora 文档;最低允许价、锁价与变动阈值由
``PricePolicyConfig`` 结构化注入。权限与资源范围属 Access Control 阶段,
此处不重复校验。
"""

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.persistence.models import Listing, Product
from agents.product_operations.persistence.repository import ProductRepository
from agents.product_operations.policy.models import (
    PricePolicyConfig,
    PricePolicyDecision,
    PriceWriteRequest,
)


LISTING_NOT_FOUND: Final = "LISTING_NOT_FOUND"
LISTING_NOT_ACTIVE: Final = "LISTING_NOT_ACTIVE"
INVALID_TARGET_PRICE: Final = "INVALID_TARGET_PRICE"
PRICE_UNCHANGED: Final = "PRICE_UNCHANGED"
CURRENCY_MISMATCH: Final = "CURRENCY_MISMATCH"
PRICE_BELOW_FLOOR: Final = "PRICE_BELOW_FLOOR"
PRICE_LOCKED: Final = "PRICE_LOCKED"
PRICE_CHANGE_LIMIT_EXCEEDED: Final = "PRICE_CHANGE_LIMIT_EXCEEDED"
LISTING_VERSION_CONFLICT: Final = "LISTING_VERSION_CONFLICT"

_DENY_MESSAGES: Final[dict[str, str]] = {
    LISTING_NOT_FOUND: "listing does not exist in this workspace",
    LISTING_NOT_ACTIVE: "listing is not active",
    INVALID_TARGET_PRICE: "target price is not a valid channel amount",
    PRICE_UNCHANGED: "target price equals current price",
    CURRENCY_MISMATCH: "target price currency does not match the listing",
    PRICE_BELOW_FLOOR: "target price is below the allowed floor",
    PRICE_LOCKED: "an active price lock forbids price changes",
    PRICE_CHANGE_LIMIT_EXCEEDED: "price change exceeds the per-update hard limit",
    LISTING_VERSION_CONFLICT: "listing version has changed since confirmation",
}

# 渠道小数位规则:金额最多两位小数(与 Numeric(18, 2) 列一致)。
_MAX_DECIMAL_PLACES: Final = 2


class PricePolicy:
    """价格业务策略:只读 PostgreSQL,返回 PricePolicyDecision 值对象。"""

    def __init__(self, session: AsyncSession, config: PricePolicyConfig) -> None:
        self._session = session
        self._config = config
        self._repository = ProductRepository(session)

    async def evaluate(self, request: PriceWriteRequest) -> PricePolicyDecision:
        """按固定次序校验写请求;全部通过返回 allowed 决策。"""
        listing = await self._resolve_listing(request)
        if listing is None:
            return self._deny(request, LISTING_NOT_FOUND)
        if listing.status is not ListingStatus.ACTIVE:
            return self._deny(request, LISTING_NOT_ACTIVE, listing=listing)

        price_issue = self._check_price_amount(request, listing)
        if price_issue is not None:
            return self._deny(request, price_issue, listing=listing)

        if request.target_price < self._config.min_allowed_price:
            return self._deny(request, PRICE_BELOW_FLOOR, listing=listing)

        price_lock = await self._repository.get_active_price_lock(
            request.workspace_id, listing.id
        )
        if price_lock is not None:
            return self._deny(request, PRICE_LOCKED, listing=listing)

        if abs(request.target_price - listing.current_price) > self._config.max_price_change:
            return self._deny(request, PRICE_CHANGE_LIMIT_EXCEEDED, listing=listing)

        if request.expected_version != listing.version:
            return self._deny(request, LISTING_VERSION_CONFLICT, listing=listing)

        return PricePolicyDecision(
            allowed=True,
            policy_version=self._config.policy_version,
            listing_version=listing.version,
            expected_new_price=request.target_price,
        )

    async def _resolve_listing(self, request: PriceWriteRequest) -> Listing | None:
        """按 listing_id(服务端补全)或 store_id + sku(模型输入)解析 Listing。

        跨 Workspace 不可见;``listing_id`` 命中的行若不属于请求的
        ``store_id`` 视同不存在,不泄漏跨店引用。
        """
        if request.listing_id is not None:
            listing = await self._repository.get_listing(request.workspace_id, request.listing_id)
            if listing is not None and listing.store_id != request.store_id:
                return None
            return listing
        if request.sku is None:
            return None
        stmt = (
            select(Listing)
            .join(Product, Listing.product_id == Product.id)
            .where(
                Listing.workspace_id == request.workspace_id,
                Listing.store_id == request.store_id,
                Product.sku == request.sku,
            )
        )
        return (await self._session.scalars(stmt)).one_or_none()

    def _check_price_amount(self, request: PriceWriteRequest, listing: Listing) -> str | None:
        """Decimal 合法性与币种(架构文档 §6 第 3 条);返回 reason_code 或 None。"""
        target = request.target_price
        # NaN/Infinity 先短路,避免比较运算抛 InvalidOperation;
        # 小数位以 exponent 判断,quantize 对超精度值会抛异常。
        exponent = target.as_tuple().exponent
        if (
            not target.is_finite()
            or target <= 0
            or (isinstance(exponent, int) and exponent < -_MAX_DECIMAL_PLACES)
        ):
            return INVALID_TARGET_PRICE
        if target == listing.current_price:
            return PRICE_UNCHANGED
        if listing.currency != self._config.currency:
            return CURRENCY_MISMATCH
        return None

    def _deny(
        self,
        request: PriceWriteRequest,
        reason_code: str,
        *,
        listing: Listing | None = None,
    ) -> PricePolicyDecision:
        return PricePolicyDecision(
            allowed=False,
            reason_code=reason_code,
            safe_message=_DENY_MESSAGES[reason_code],
            policy_version=self._config.policy_version,
            listing_version=listing.version if listing is not None else None,
            expected_new_price=request.target_price,
        )


__all__ = [
    "CURRENCY_MISMATCH",
    "INVALID_TARGET_PRICE",
    "LISTING_NOT_ACTIVE",
    "LISTING_NOT_FOUND",
    "LISTING_VERSION_CONFLICT",
    "PRICE_BELOW_FLOOR",
    "PRICE_CHANGE_LIMIT_EXCEEDED",
    "PRICE_LOCKED",
    "PRICE_UNCHANGED",
    "PricePolicy",
]
