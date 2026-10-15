"""ProductOperations ToolVersion 的声明式 Binding 工厂。

本模块把 Catalog、Pricing、Inventory、Listing、Runtime Control 与历史兼容绑定
分别构造后展平。主组合根只负责把返回值登记进 Registry，不再按 stable key 分支。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.commerce.protocol import CommerceAdapter
from adapters.commerce.protocol import ExecutionReceipt as AdapterExecutionReceipt
from adapters.commerce.protocol import UpdatePriceCommand as AdapterPriceCommand
from agents.product_operations.catalog.tools import (
    SEARCH_PRODUCTS_DEFINITION,
    search_products,
)
from agents.product_operations.domain.models import StoreStatus
from agents.product_operations.input.profiles import (
    TOOL_ARGUMENT_FIELDS,
    p1_fields_for,
    product_registration,
)
from agents.product_operations.inventory.gateway import InventoryGateway
from agents.product_operations.inventory.policy import InventoryPolicyConfig
from agents.product_operations.inventory.service import AdjustInventoryQuantityHandler
from agents.product_operations.inventory.tools import (
    ADJUST_INVENTORY_QUANTITY_DEFINITION,
    GET_INVENTORY_LEVEL_DEFINITION,
    get_inventory_level,
)
from agents.product_operations.listing.gateway import ListingGateway
from agents.product_operations.listing.policy import ListingPolicyConfig
from agents.product_operations.listing.service import (
    PublishListingHandler,
    UnpublishListingHandler,
)
from agents.product_operations.listing.tools import (
    GET_LISTING_DEFINITION,
    PUBLISH_LISTING_DEFINITION,
    UNPUBLISH_LISTING_DEFINITION,
    get_listing,
)
from agents.product_operations.persistence.models import Store
from agents.product_operations.policy.price_pretool import PricePreToolPolicy
from agents.product_operations.policy.resources import (
    STORE_RESOURCE_TYPE,
    StoreIdsResourceResolver,
    StoreResourceResolver,
)
from agents.product_operations.pricing.configuration import PublishedPricingPolicy
from agents.product_operations.pricing.gateway import PricingGateway
from agents.product_operations.pricing.service import SetListingPriceHandler
from agents.product_operations.pricing.tools import (
    COMPARE_LISTING_PRICES_DEFINITION,
    SET_LISTING_PRICE_DEFINITION,
    compare_listing_prices,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION as LEGACY_COMPARE_LISTING_PRICES_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    GET_PRODUCT_SNAPSHOT_DEFINITION as LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    SEARCH_PRODUCTS_DEFINITION as LEGACY_SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.handlers import (
    CompareListingPricesHandler,
    GetProductSnapshotHandler,
    SearchProductsHandler,
)
from agents.product_operations.tools.update_price import (
    STABLE_KEY_UPDATE_PRICE,
    UPDATE_PRICE_DEFINITION,
    UpdatePriceWriteCommand,
    UpdatePriceWriteReceipt,
)
from composition.provisioning.product_operations import ProductOperationsCapabilities
from runtime.access.actor import ActorContext, ActorContextBuilder
from runtime.capabilities.registry import ToolBinding, VersionedToolBinding
from runtime.capabilities.skill_activation import (
    LOAD_SKILL_DEFINITION,
    LOAD_SKILL_HANDLER,
    LOAD_SKILL_KEY,
)
from runtime.execution.bindings import ToolExecutionProfile
from runtime.execution.pretool.decisions import PolicyDecision
from runtime.execution.pretool.profiles import (
    PreToolProfileContext,
    PreToolProfileRegistration,
    runtime_control_registration,
)
from runtime.execution.tool_result_processor import ToolExecutionReceipt
from runtime.persistence.base import ConfirmationRequestStatus
from runtime.persistence.models.interaction import ConfirmationRequest


_WRITE_ADAPTER_CAPABILITIES = frozenset({"operation", "reconciliation"})

ReadInvoker = Callable[
    [ActorContext, AsyncSession, Mapping[str, Any]], Awaitable[Any]
]
WriteHandlerFactory = Callable[[ActorContext, AsyncSession], Any]


class LegacyCommerceGateway:
    """第一版 ``update_price`` Handler 到兼容 CommerceAdapter 协议的桥接。"""

    def __init__(self, adapter: CommerceAdapter) -> None:
        self._adapter = adapter

    async def execute(
        self, command: UpdatePriceWriteCommand
    ) -> UpdatePriceWriteReceipt:
        receipt: AdapterExecutionReceipt = await self._adapter.execute(
            AdapterPriceCommand(
                operation_id=command.operation_id,
                workspace_id=command.workspace_id,
                store_id=command.store_id,
                listing_id=command.listing_id,
                external_store_ref=command.external_store_ref,
                external_product_ref=command.external_product_ref,
                target_price=command.target_price,
                currency=command.currency,
                expected_version=command.expected_version,
            )
        )
        return UpdatePriceWriteReceipt(
            status=receipt.status.value,
            before_price=receipt.before_price,
            after_price=receipt.after_price,
            listing_version=receipt.listing_version,
            external_ref=receipt.external_ref,
            reason_code=receipt.reason_code,
            retryable=receipt.retryable,
        )


@dataclass(frozen=True)
class ProductOperationsBindingDependencies:
    """构造全部 current/compatibility Binding 所需的显式依赖。"""

    session_factory: Callable[[], AsyncSession]
    workspace_id: uuid.UUID
    actor_id: uuid.UUID
    adapter_session: AsyncSession
    pricing_gateway: PricingGateway
    inventory_gateway: InventoryGateway
    listing_gateway: ListingGateway
    pricing_policy: PublishedPricingPolicy
    inventory_policy: InventoryPolicyConfig
    listing_policy: ListingPolicyConfig
    capabilities: ProductOperationsCapabilities
    adapter_key: str
    legacy_update_handler: Any


class _ReadHandler:
    """把领域只读函数适配到 Runtime ``read`` 协议。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        invoke: ReadInvoker,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._invoke = invoke

    async def read(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        deadline: datetime,
    ) -> ToolExecutionReceipt:
        del deadline
        session = self._session_factory()
        try:
            actor = await ActorContextBuilder(session).build(
                actor_id=self._actor_id,
                workspace_id=self._workspace_id,
                request_id=f"read-{tool_call_id}",
            )
            result = await self._invoke(actor, session, normalized_arguments)
            return ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status="OK",
                payload=result.model_dump(mode="json"),
            )
        finally:
            await session.close()


class _ScopedWriteHandler:
    """每次 prepare/execute 都从当前成员事实构造 Actor 与领域 Handler。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        factory: WriteHandlerFactory,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._actor_id = actor_id
        self._factory = factory

    async def _build(self, request_id: str) -> Any:
        actor = await ActorContextBuilder(self._session).build(
            actor_id=self._actor_id,
            workspace_id=self._workspace_id,
            request_id=request_id,
        )
        await self._session.rollback()
        return self._factory(actor, self._session)

    async def prepare(self, *, normalized_arguments: Mapping[str, Any]) -> Any:
        handler = await self._build(f"write-prepare-{uuid.uuid4()}")
        return await handler.prepare(normalized_arguments=normalized_arguments)

    async def execute_prepared(
        self,
        *,
        tool_call_id: uuid.UUID,
        operation_id: uuid.UUID,
        idempotency_key: str,
        prepared: Any,
    ) -> Any:
        handler = await self._build(f"write-execute-{tool_call_id}")
        return await handler.execute_prepared(
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            prepared=prepared,
        )


class _StoreReferenceAuthorizationResolver:
    """把 ``store_ref`` 解析成内部 Store 资源，交给 Runtime Authorizer。"""

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    async def resolve(
        self, *, arguments: Mapping[str, Any]
    ) -> frozenset[tuple[str, str]]:
        raw = arguments.get("store_ref")
        if not isinstance(raw, str) or not raw.strip():
            return frozenset()
        reference = raw.strip()
        stores = (
            await self._session.scalars(
                select(Store).where(
                    Store.workspace_id == self._workspace_id,
                    Store.status == StoreStatus.ACTIVE,
                )
            )
        ).all()
        matches = [
            store
            for store in stores
            if reference == store.name or reference in tuple(store.aliases)
        ]
        if len(matches) != 1:
            return frozenset()
        return frozenset({(STORE_RESOURCE_TYPE, str(matches[0].id))})


class _PreparedWriteBusinessPolicy:
    """复用领域 Handler 的 prepare 作为写前确定性业务校验。"""

    def __init__(
        self,
        *,
        handler: Any,
        session: AsyncSession,
        tool_call_id: uuid.UUID,
        policy_revision: int | None,
    ) -> None:
        self._handler = handler
        self._session = session
        self._tool_call_id = tool_call_id
        self._policy_revision = policy_revision

    async def evaluate(
        self,
        *,
        stable_key: str,
        arguments: Mapping[str, Any],
        resources: frozenset[tuple[str, str]],
    ) -> PolicyDecision:
        del stable_key, resources
        prepared = await self._handler.prepare(normalized_arguments=arguments)
        if prepared.rejection is not None:
            detail = prepared.rejection.context.get("detail")
            rule_code = prepared.rejection.context.get("rule_code")
            return PolicyDecision(
                allowed=False,
                reason_code=(
                    str(rule_code)
                    if isinstance(rule_code, str) and rule_code
                    else prepared.rejection.error_code
                ),
                safe_message=str(detail) if detail is not None else "业务校验拒绝",
                policy_revision=self._policy_revision,
            )
        expected = prepared.expected_state
        assert expected is not None
        versions = _resource_versions(expected)
        accepted = (
            await self._session.scalars(
                select(ConfirmationRequest.resource_versions)
                .where(
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
        if accepted and dict(accepted) != versions:
            return PolicyDecision(
                allowed=False,
                reason_code="LISTING_VERSION_CONFLICT",
                safe_message="resource version has changed since confirmation",
                policy_revision=self._policy_revision,
            )
        return PolicyDecision(
            allowed=True,
            resource_versions=versions,
            policy_revision=self._policy_revision,
        )


def _resource_versions(expected_state: Mapping[str, Any]) -> dict[str, int]:
    version = int(expected_state["expected_version"])
    listing_id = expected_state.get("listing_id")
    if listing_id is not None:
        return {f"listing:{listing_id}": version}
    return {
        "inventory:"
        f"{expected_state['location_id']}:{expected_state['product_id']}": version
    }


def _write_registration(
    *,
    key: str,
    factory: WriteHandlerFactory,
    policy_revision: int | None,
) -> PreToolProfileRegistration:
    def business_policy(context: PreToolProfileContext) -> _PreparedWriteBusinessPolicy:
        return _PreparedWriteBusinessPolicy(
            handler=factory(context.actor, context.session),
            session=context.session,
            tool_call_id=context.request.tool_call_id,
            policy_revision=policy_revision,
        )

    return product_registration(
        fields=p1_fields_for(key),
        policy_mode="BUSINESS",
        resource_mode="RESOURCE",
        business_policy_factory=business_policy,
        resource_resolver_factory=lambda context: _StoreReferenceAuthorizationResolver(
            context.session, context.actor.workspace_id
        ),
    )


def _versioned(
    *,
    version_id: uuid.UUID,
    definition: Any,
    handler: Any,
    registration: PreToolProfileRegistration,
    make_current: bool,
    execution_profile: ToolExecutionProfile | None = None,
) -> VersionedToolBinding:
    return VersionedToolBinding(
        version_id=version_id,
        binding=ToolBinding(
            definition=definition,
            handler=handler,
            pretool_registration=registration,
            execution_profile=execution_profile,
            write_adapter_capabilities=(
                _WRITE_ADAPTER_CAPABILITIES
                if execution_profile is not None
                else frozenset()
            ),
        ),
        make_current=make_current,
    )


def _string_sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(str(item) for item in value)
    return ()


async def _search(
    actor: ActorContext, session: AsyncSession, arguments: Mapping[str, Any]
) -> Any:
    query = arguments.get("query")
    return await search_products(
        actor=actor,
        session=session,
        query=query if isinstance(query, str) else None,
        store_refs=_string_sequence(arguments.get("store_refs")),
    )


async def _get_listing(
    actor: ActorContext, session: AsyncSession, arguments: Mapping[str, Any]
) -> Any:
    return await get_listing(
        actor=actor,
        session=session,
        store_ref=str(arguments.get("store_ref", "")),
        sku=str(arguments.get("sku", "")),
    )


async def _compare(
    actor: ActorContext, session: AsyncSession, arguments: Mapping[str, Any]
) -> Any:
    return await compare_listing_prices(
        actor=actor,
        session=session,
        sku=str(arguments.get("sku", "")),
        store_refs=_string_sequence(arguments.get("store_refs")),
    )


async def _get_inventory(
    actor: ActorContext, session: AsyncSession, arguments: Mapping[str, Any]
) -> Any:
    location_ref = arguments.get("location_ref")
    return await get_inventory_level(
        actor=actor,
        session=session,
        store_ref=str(arguments.get("store_ref", "")),
        sku=str(arguments.get("sku", "")),
        location_ref=location_ref if isinstance(location_ref, str) else None,
    )


def build_catalog_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    return (
        _versioned(
            version_id=deps.capabilities.tool_version_ids["search_products"],
            definition=SEARCH_PRODUCTS_DEFINITION,
            handler=_ReadHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                invoke=_search,
            ),
            registration=product_registration(
                fields=p1_fields_for("search_products"),
                policy_mode="NONE",
                resource_mode="WORKSPACE",
            ),
            make_current=True,
        ),
    )


def build_pricing_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    factory: WriteHandlerFactory = lambda actor, session: SetListingPriceHandler(
        actor=actor,
        session=session,
        gateway=deps.pricing_gateway,
        pricing_policy=deps.pricing_policy,
    )
    profile = ToolExecutionProfile(
        adapter_key=deps.adapter_key, supports_reconciliation=True
    )
    return (
        _versioned(
            version_id=deps.capabilities.tool_version_ids["compare_listing_prices"],
            definition=COMPARE_LISTING_PRICES_DEFINITION,
            handler=_ReadHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                invoke=_compare,
            ),
            registration=product_registration(
                fields=p1_fields_for("compare_listing_prices"),
                policy_mode="NONE",
                resource_mode="WORKSPACE",
            ),
            make_current=True,
        ),
        _versioned(
            version_id=deps.capabilities.tool_version_ids["set_listing_price"],
            definition=SET_LISTING_PRICE_DEFINITION,
            handler=_ScopedWriteHandler(
                session=deps.adapter_session,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                factory=factory,
            ),
            registration=_write_registration(
                key="set_listing_price",
                factory=factory,
                policy_revision=deps.pricing_policy.version,
            ),
            make_current=True,
            execution_profile=profile,
        ),
    )


def build_inventory_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    factory: WriteHandlerFactory = lambda actor, session: AdjustInventoryQuantityHandler(
        actor=actor,
        session=session,
        gateway=deps.inventory_gateway,
        policy_config=deps.inventory_policy,
    )
    profile = ToolExecutionProfile(
        adapter_key=deps.adapter_key, supports_reconciliation=True
    )
    return (
        _versioned(
            version_id=deps.capabilities.tool_version_ids["get_inventory_level"],
            definition=GET_INVENTORY_LEVEL_DEFINITION,
            handler=_ReadHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                invoke=_get_inventory,
            ),
            registration=product_registration(
                fields=p1_fields_for("get_inventory_level"),
                policy_mode="NONE",
                resource_mode="WORKSPACE",
            ),
            make_current=True,
        ),
        _versioned(
            version_id=deps.capabilities.tool_version_ids[
                "adjust_inventory_quantity"
            ],
            definition=ADJUST_INVENTORY_QUANTITY_DEFINITION,
            handler=_ScopedWriteHandler(
                session=deps.adapter_session,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                factory=factory,
            ),
            registration=_write_registration(
                key="adjust_inventory_quantity",
                factory=factory,
                policy_revision=None,
            ),
            make_current=True,
            execution_profile=profile,
        ),
    )


def build_listing_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    publish_factory: WriteHandlerFactory = lambda actor, session: PublishListingHandler(
        actor=actor,
        session=session,
        gateway=deps.listing_gateway,
        policy_config=deps.listing_policy,
    )
    unpublish_factory: WriteHandlerFactory = (
        lambda actor, session: UnpublishListingHandler(
            actor=actor,
            session=session,
            gateway=deps.listing_gateway,
            policy_config=deps.listing_policy,
        )
    )
    profile = ToolExecutionProfile(
        adapter_key=deps.adapter_key, supports_reconciliation=True
    )
    return (
        _versioned(
            version_id=deps.capabilities.tool_version_ids["get_listing"],
            definition=GET_LISTING_DEFINITION,
            handler=_ReadHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                invoke=_get_listing,
            ),
            registration=product_registration(
                fields=p1_fields_for("get_listing"),
                policy_mode="NONE",
                resource_mode="WORKSPACE",
            ),
            make_current=True,
        ),
        _versioned(
            version_id=deps.capabilities.tool_version_ids["publish_listing"],
            definition=PUBLISH_LISTING_DEFINITION,
            handler=_ScopedWriteHandler(
                session=deps.adapter_session,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                factory=publish_factory,
            ),
            registration=_write_registration(
                key="publish_listing",
                factory=publish_factory,
                policy_revision=None,
            ),
            make_current=True,
            execution_profile=profile,
        ),
        _versioned(
            version_id=deps.capabilities.tool_version_ids["unpublish_listing"],
            definition=UNPUBLISH_LISTING_DEFINITION,
            handler=_ScopedWriteHandler(
                session=deps.adapter_session,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
                factory=unpublish_factory,
            ),
            registration=_write_registration(
                key="unpublish_listing",
                factory=unpublish_factory,
                policy_revision=None,
            ),
            make_current=True,
            execution_profile=profile,
        ),
    )


def build_runtime_control_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    return (
        _versioned(
            version_id=deps.capabilities.tool_version_ids[LOAD_SKILL_KEY],
            definition=LOAD_SKILL_DEFINITION,
            handler=LOAD_SKILL_HANDLER,
            registration=runtime_control_registration(),
            make_current=True,
        ),
    )


def build_legacy_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    version_ids = deps.capabilities.compatibility_tool_version_ids
    profile = ToolExecutionProfile(
        adapter_key=deps.adapter_key, supports_reconciliation=True
    )
    price_policy_factory = lambda context: PricePreToolPolicy(
        session=context.session,
        config=deps.pricing_policy.config,
        actor=context.actor,
        tool_call_id=context.request.tool_call_id,
        policy_revision=deps.pricing_policy.version,
    )
    return (
        _versioned(
            version_id=version_ids["search_products"][0],
            definition=LEGACY_SEARCH_PRODUCTS_DEFINITION,
            handler=SearchProductsHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
            ),
            registration=product_registration(
                fields=TOOL_ARGUMENT_FIELDS["search_products"],
                policy_mode="NONE",
                resource_mode="WORKSPACE",
            ),
            make_current=False,
        ),
        _versioned(
            version_id=version_ids["get_product_snapshot"][0],
            definition=LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION,
            handler=GetProductSnapshotHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
            ),
            registration=product_registration(
                fields=TOOL_ARGUMENT_FIELDS["get_product_snapshot"],
                policy_mode="NONE",
                resource_mode="RESOURCE",
                resource_resolver=StoreResourceResolver(),
            ),
            make_current=False,
        ),
        _versioned(
            version_id=version_ids["compare_listing_prices"][0],
            definition=LEGACY_COMPARE_LISTING_PRICES_DEFINITION,
            handler=CompareListingPricesHandler(
                session_factory=deps.session_factory,
                workspace_id=deps.workspace_id,
                actor_id=deps.actor_id,
            ),
            registration=product_registration(
                fields=TOOL_ARGUMENT_FIELDS["compare_listing_prices"],
                policy_mode="NONE",
                resource_mode="RESOURCE",
                resource_resolver=StoreIdsResourceResolver(),
            ),
            make_current=False,
        ),
        _versioned(
            version_id=version_ids[STABLE_KEY_UPDATE_PRICE][0],
            definition=UPDATE_PRICE_DEFINITION,
            handler=deps.legacy_update_handler,
            registration=product_registration(
                fields=TOOL_ARGUMENT_FIELDS[STABLE_KEY_UPDATE_PRICE],
                policy_mode="BUSINESS",
                resource_mode="RESOURCE",
                business_policy_factory=price_policy_factory,
                resource_resolver=StoreResourceResolver(),
            ),
            make_current=False,
            execution_profile=profile,
        ),
    )


def flatten_versioned_bindings(
    groups: Sequence[Sequence[VersionedToolBinding]],
) -> tuple[VersionedToolBinding, ...]:
    """按声明顺序展平并拒绝重复 Version ID。"""
    flattened = tuple(binding for group in groups for binding in group)
    version_ids = [binding.version_id for binding in flattened]
    if len(set(version_ids)) != len(version_ids):
        raise ValueError("duplicate ToolVersion ID in product bindings")
    return flattened


def build_product_tool_bindings(
    deps: ProductOperationsBindingDependencies,
) -> tuple[VersionedToolBinding, ...]:
    """构造 P1 current 与第一版 compatibility 的全部声明式绑定。"""
    return flatten_versioned_bindings(
        (
            build_catalog_bindings(deps),
            build_pricing_bindings(deps),
            build_inventory_bindings(deps),
            build_listing_bindings(deps),
            build_runtime_control_bindings(deps),
            build_legacy_bindings(deps),
        )
    )


__all__ = [
    "LegacyCommerceGateway",
    "ProductOperationsBindingDependencies",
    "build_product_tool_bindings",
    "flatten_versioned_bindings",
]
