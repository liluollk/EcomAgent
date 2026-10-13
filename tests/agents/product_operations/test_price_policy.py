"""价格 Policy 与 store 资源解析测试(计划 03 Task 4;架构文档 §5、§6)。

覆盖:
- 计划原文用例:below_floor/active_lock/change_too_large 三类拒绝及 reason_code;
- 校验次序(架构文档 §6):Listing 存在与状态 → Decimal/币种 → 最低允许价
  → 活动锁价 → 单次变动硬阈值 → Listing Version;
- allowed 路径携带 PolicyDecision 值对象(policy_version/listing_version/
  expected_new_price),且决策对象 frozen;
- AuthorizationResourceResolver 只返回 ``("store", store_id)`` 资源引用;
- Policy 不读 Prompt/Memory:evaluate 输入仅为结构化数据(结构断言)。
"""

import inspect
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Final

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.persistence.models import Listing
from agents.product_operations.policy.models import (
    PricePolicyConfig,
    PriceWriteRequest,
)
from agents.product_operations.policy.price_policy import PricePolicy
from agents.product_operations.policy.resources import StoreResourceResolver
from runtime.access.actor import ActorContext
from runtime.execution.pretool.decisions import PolicyDecision
from runtime.interfaces.identifiers import ResourceScope
from runtime.persistence.base import MembershipRole


PRICE_POLICY_CONFIG: Final = PricePolicyConfig(
    policy_version="price-policy-v1",
    currency="CNY",
    min_allowed_price=Decimal("59.00"),
    max_price_change=Decimal("50.00"),
)

DEFAULT_CURRENT_PRICE: Final = Decimal("128.00")
SAFE_TARGET_PRICE: Final = Decimal("138.00")


@pytest.fixture()
def price_policy_config() -> PricePolicyConfig:
    return PRICE_POLICY_CONFIG


@pytest.fixture()
def price_policy(
    db_session: AsyncSession, price_policy_config: PricePolicyConfig
) -> PricePolicy:
    return PricePolicy(db_session, price_policy_config)


@pytest.fixture()
def actor_context(product_seed) -> ActorContext:
    """具备 product.price.write 的运营者;资源范围交由 Access Control 校验。"""
    return ActorContext(
        actor_id=uuid.uuid4(),
        workspace_id=product_seed.workspace.id,
        membership_id=uuid.uuid4(),
        role=MembershipRole.OPERATOR,
        permissions=frozenset({"product.price.write"}),
        resource_scopes={
            "store": ResourceScope(
                mode="SELECTED",
                resource_ids=frozenset({str(product_seed.store.id)}),
            ),
        },
        request_id="req-price-policy",
    )


@pytest.fixture()
def price_case(
    product_seed,
    db_session: AsyncSession,
    actor_context: ActorContext,
) -> Callable[[str], Awaitable[PriceWriteRequest]]:
    """按用例名构造 update_price 写请求值对象。

    种子默认带活动锁价;除 ``active_lock`` 外先删除锁,避免锁检查遮蔽其它
    校验分支。``below_floor`` 同时超出单次变动阈值,用于证明校验次序中
    最低允许价先于变动阈值。
    """

    async def _build(case: str) -> PriceWriteRequest:
        listing = product_seed.listing
        if case != "active_lock":
            await db_session.delete(product_seed.price_lock)
            await db_session.flush()
        if case == "listing_inactive":
            listing.status = ListingStatus.UNPUBLISHED
            await db_session.flush()
        if case == "currency_mismatch":
            listing.currency = "USD"
            await db_session.flush()
        target = {
            "below_floor": Decimal("19.99"),
            "change_too_large": Decimal("320.00"),
            "invalid_price_non_positive": Decimal("0.00"),
            "too_many_decimals": Decimal("138.999"),
            "unchanged_price": DEFAULT_CURRENT_PRICE,
        }.get(case, SAFE_TARGET_PRICE)
        expected_version = listing.version + (1 if case == "version_conflict" else 0)
        if case == "listing_not_found":
            return PriceWriteRequest(
                workspace_id=product_seed.workspace.id,
                store_id=product_seed.store.id,
                listing_id=uuid.uuid4(),
                target_price=target,
                expected_version=expected_version,
                actor=actor_context,
            )
        return PriceWriteRequest(
            workspace_id=product_seed.workspace.id,
            store_id=product_seed.store.id,
            sku=product_seed.product.sku,
            target_price=target,
            expected_version=expected_version,
            actor=actor_context,
        )

    return _build


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("below_floor", "PRICE_BELOW_FLOOR"),
        ("active_lock", "PRICE_LOCKED"),
        ("change_too_large", "PRICE_CHANGE_LIMIT_EXCEEDED"),
    ],
)
async def test_price_policy_denies_unsafe_writes(
    price_policy: PricePolicy,
    price_case: Callable[[str], Awaitable[PriceWriteRequest]],
    case: str,
    reason: str,
) -> None:
    decision = await price_policy.evaluate(await price_case(case))

    assert decision.allowed is False
    assert decision.reason_code == reason


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("listing_not_found", "LISTING_NOT_FOUND"),
        ("listing_inactive", "LISTING_NOT_ACTIVE"),
        ("invalid_price_non_positive", "INVALID_TARGET_PRICE"),
        ("too_many_decimals", "INVALID_TARGET_PRICE"),
        ("unchanged_price", "PRICE_UNCHANGED"),
        ("currency_mismatch", "CURRENCY_MISMATCH"),
        ("version_conflict", "LISTING_VERSION_CONFLICT"),
    ],
)
async def test_price_policy_denies_structurally_invalid_writes(
    price_policy: PricePolicy,
    price_case: Callable[[str], Awaitable[PriceWriteRequest]],
    case: str,
    reason: str,
) -> None:
    decision = await price_policy.evaluate(await price_case(case))

    assert decision.allowed is False
    assert decision.reason_code == reason


async def test_price_policy_allows_safe_price_change(
    price_policy: PricePolicy,
    price_case: Callable[[str], Awaitable[PriceWriteRequest]],
    listing: Listing,
    price_policy_config: PricePolicyConfig,
) -> None:
    decision = await price_policy.evaluate(await price_case("allowed"))

    assert decision.allowed is True
    assert decision.reason_code is None
    assert isinstance(decision, PolicyDecision)
    assert decision.policy_version == price_policy_config.policy_version
    assert decision.listing_version == listing.version
    assert decision.expected_new_price == SAFE_TARGET_PRICE


async def test_price_policy_decision_is_frozen_value_object(
    price_policy: PricePolicy,
    price_case: Callable[[str], Awaitable[PriceWriteRequest]],
) -> None:
    decision = await price_policy.evaluate(await price_case("allowed"))

    with pytest.raises(ValidationError):
        decision.allowed = False


async def test_store_resource_resolver_returns_only_store_reference(product_seed) -> None:
    resolver = StoreResourceResolver()

    resources = await resolver.resolve(
        arguments={
            "store_id": str(product_seed.store.id),
            "sku": product_seed.product.sku,
            "target_price": "138.00",
            "expected_version": 1,
        }
    )

    assert resources == frozenset({("store", str(product_seed.store.id))})


@pytest.mark.parametrize(
    "arguments",
    [{}, {"sku": "SKU-1"}, {"store_id": "not-a-uuid"}, {"store_id": 123}],
)
async def test_store_resource_resolver_omits_missing_or_invalid_store_id(
    arguments: dict[str, object],
) -> None:
    resolver = StoreResourceResolver()

    resources = await resolver.resolve(arguments=arguments)

    assert resources == frozenset()


def test_price_policy_inputs_are_structured_not_prompt_derived() -> None:
    """Policy 不读 Prompt/Memory:evaluate 仅接收写请求值对象,配置全为结构化字段。"""
    assert list(inspect.signature(PricePolicy.evaluate).parameters) == ["self", "request"]
    assert {"prompt", "memory"}.isdisjoint(PricePolicyConfig.model_fields)
