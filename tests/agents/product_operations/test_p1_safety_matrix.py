"""P1 四写工具统一安全矩阵(Task 14 Step 2)。

按计划要求对四个写 Tool(``set_listing_price`` / ``adjust_inventory_quantity``
/ ``publish_listing`` / ``unpublish_listing``)参数化验证七类场景:无写权限、
跨店铺、缺少关键范围、确认后版本变化、响应丢失、重复 operation_id、
Adapter 不支持能力。

价格单工具的深度矩阵(锁价、下限价等价格特有规则)见
``test_safety_matrix.py``;本文件验证同一套守卫语义在四个写工具上均匀成立:

- 未授权写一律 ``DENIED`` / ``PERMISSION_DENIED``,权威状态不动;
- 越权店铺与空资源范围一律 ``DENIED`` / ``RESOURCE_SCOPE_DENIED``;
- 确认后目标行版本变化一律拦截(策略预检或执行期 CAS),权威状态不动;
- 响应丢失经 reconcile 恰好恢复一次权威写;
- 同 operation_id 重放被平台幂等吸收,逻辑写不增加;
- REST Adapter(仅声明 pricing)上的库存/刊登写失败闭合,不构造猜测 URL。

权威状态:价格读 ``listings.current_price``,库存读
``inventory_levels.available_quantity``,刊登读 ``listings.status``。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from adapters.commerce.rest_adapter import RestCommerceAdapter
from agents.product_operations.domain.models import ListingStatus, StorePlatform
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
)
from agents.product_operations.inventory.gateway import (
    derive_idempotency_key as derive_inventory_idempotency_key,
)
from agents.product_operations.listing.gateway import (
    ChangePublicationCommand,
)
from agents.product_operations.listing.gateway import (
    derive_idempotency_key as derive_publication_idempotency_key,
)
from agents.product_operations.persistence.models import (
    InventoryLevel,
    Listing,
    Store,
)
from agents.product_operations.pricing.gateway import SetListingPriceCommand
from apps.worker.assembly import ProductApplication
from runtime.backend.protocol import BackendCompleted, ToolCallCompleted
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import ResourceScopeMode, ToolCallStatus
from runtime.persistence.models.execution import Operation
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
)


BASE_PRICE: Final = Decimal("128.00")
TARGET_PRICE: Final = Decimal("99.00")
BASE_QUANTITY: Final = 20
ADJUSTED_QUANTITY: Final = 25
DEFAULT_STORE_REF: Final = "淘宝旗舰店"
OUT_OF_SCOPE_STORE_REF: Final = "抖音旗舰店"


@dataclass(frozen=True)
class _ToolSpec:
    """一个写工具的矩阵参数:参数、权限、消息模板与版本冲突预期。"""

    tool: str
    permission: str
    arguments: dict[str, Any]
    message: str
    conflict_code: str
    conflict_status: ToolCallStatus
    targets_inventory: bool


_TOOLS: Final[tuple[_ToolSpec, ...]] = (
    _ToolSpec(
        tool="set_listing_price",
        permission="product.price.write",
        arguments={"sku": "SKU-001", "target_price": "99.00"},
        message="把{store} SKU-001 改成 99 元",
        conflict_code="LISTING_VERSION_CONFLICT",
        conflict_status=ToolCallStatus.DENIED,
        targets_inventory=False,
    ),
    _ToolSpec(
        tool="adjust_inventory_quantity",
        permission="product.inventory.write",
        arguments={"sku": "SKU-001", "delta": 5, "reason": "RECEIPT"},
        message="把{store}的 SKU-001 库存补 5 件",
        conflict_code="LISTING_VERSION_CONFLICT",
        conflict_status=ToolCallStatus.DENIED,
        targets_inventory=True,
    ),
    _ToolSpec(
        tool="publish_listing",
        permission="product.listing.write",
        arguments={"sku": "SKU-001"},
        message="上架{store}的 SKU-001",
        conflict_code="LISTING_VERSION_CONFLICT",
        conflict_status=ToolCallStatus.DENIED,
        targets_inventory=False,
    ),
    _ToolSpec(
        tool="unpublish_listing",
        permission="product.listing.write",
        arguments={"sku": "SKU-001", "reason": "MANUAL"},
        message="下架{store}的 SKU-001",
        conflict_code="LISTING_VERSION_CONFLICT",
        conflict_status=ToolCallStatus.DENIED,
        targets_inventory=False,
    ),
)


@dataclass(frozen=True)
class _Outcome:
    """单个场景的权威统计:ToolCall 状态、失败码、逻辑写数、权威终态。"""

    tool_call_status: ToolCallStatus
    failure_code: str | None
    authoritative_write_count: int
    authoritative_price: Decimal
    authoritative_quantity: int
    authoritative_publication_state: ListingStatus


class MatrixRunner:
    """矩阵驱动器:布置场景 → 执行闭环 → 统计权威结果。"""

    def __init__(
        self, *, application: ProductApplication, engine: AsyncEngine
    ) -> None:
        self._application = application
        self._engine = engine

    @property
    def application(self) -> ProductApplication:
        return self._application

    @property
    def ids(self):
        return self._application.seed_ids

    # -- 场景环境布置(独立短事务,立即提交) ----------------------------

    async def _mutate(self, action: Any) -> None:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            async with session.begin():
                await action(session)
        finally:
            await session.close()

    async def grant_write_permission(self, spec: _ToolSpec) -> None:
        """授予该工具写权限(幂等:价格写权限在基线种子中已存在)。"""
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.delete(MembershipPermission).where(
                    MembershipPermission.workspace_id == ids.workspace_id,
                    MembershipPermission.membership_id == ids.membership_id,
                    MembershipPermission.permission == spec.permission,
                )
            )
            session.add(
                MembershipPermission(
                    workspace_id=ids.workspace_id,
                    membership_id=ids.membership_id,
                    permission=spec.permission,
                )
            )

        await self._mutate(action)

    async def revoke_write_permission(self, spec: _ToolSpec) -> None:
        """撤销成员的该工具写权限(价格写权限在基线种子中已授予)。"""
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.delete(MembershipPermission).where(
                    MembershipPermission.workspace_id == ids.workspace_id,
                    MembershipPermission.membership_id == ids.membership_id,
                    MembershipPermission.permission == spec.permission,
                )
            )

        await self._mutate(action)

    async def seed_out_of_scope_store(self) -> None:
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            session.add(
                Store(
                    id=uuid.uuid4(),
                    workspace_id=ids.workspace_id,
                    platform=StorePlatform.DOUYIN,
                    external_store_ref=f"douyin-{uuid.uuid4()}",
                    name=OUT_OF_SCOPE_STORE_REF,
                    aliases=[OUT_OF_SCOPE_STORE_REF],
                )
            )

        await self._mutate(action)

    async def restrict_store_scope(self, *, include_taobao: bool) -> None:
        """把成员 store 范围收窄为 SELECTED;可选择性保留淘宝旗舰店。"""
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.update(MembershipResourceScopeMode)
                .where(
                    MembershipResourceScopeMode.workspace_id == ids.workspace_id,
                    MembershipResourceScopeMode.membership_id == ids.membership_id,
                    MembershipResourceScopeMode.resource_type == "store",
                )
                .values(mode=ResourceScopeMode.SELECTED)
            )
            if include_taobao:
                session.add(
                    MembershipResourceScope(
                        workspace_id=ids.workspace_id,
                        membership_id=ids.membership_id,
                        resource_type="store",
                        resource_id=str(ids.store_id),
                    )
                )

        await self._mutate(action)

    async def bump_listing_version(self) -> None:
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.update(Listing)
                .where(
                    Listing.workspace_id == ids.workspace_id,
                    Listing.id == ids.listing_id,
                )
                .values(version=Listing.version + 1)
            )

        await self._mutate(action)

    async def bump_inventory_version(self) -> None:
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.update(InventoryLevel)
                .where(InventoryLevel.workspace_id == ids.workspace_id)
                .values(version=InventoryLevel.version + 1)
            )

        await self._mutate(action)

    # -- 驱动入口 ---------------------------------------------------------

    async def set_listing_publication(self, status: ListingStatus) -> None:
        ids = self.ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.update(Listing)
                .where(
                    Listing.workspace_id == ids.workspace_id,
                    Listing.id == ids.listing_id,
                )
                .values(status=status)
            )

        await self._mutate(action)

    async def start_write(self, spec: _ToolSpec, *, store_ref: str) -> Any:
        """脚本化一轮指定写 Tool 的 ToolCall 并发起 Run。

        发布路径把种子 Listing 切到 UNPUBLISHED(否则同目标发布是幂等
        no-op,不产生权威写,矩阵语义失真);下架路径保持默认 PUBLISHED。
        """
        if spec.tool == "publish_listing":
            await self.set_listing_publication(ListingStatus.UNPUBLISHED)
        arguments = dict(spec.arguments)
        arguments["store_ref"] = store_ref
        self._application.scripted_backend.set_rounds(
            [
                [
                    ToolCallCompleted(
                        call_id="c0", name=spec.tool, arguments=arguments
                    ),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        )
        return await self._application.start(spec.message.format(store=store_ref))

    async def outcome(self, run_id: Any, spec: _ToolSpec) -> _Outcome:
        application = self._application
        snapshot = await application.tool_call_snapshot(run_id)
        assert snapshot is not None, f"{spec.tool} produced no tool call"
        writes = 0
        counter = getattr(application.adapter, "count_logical_writes", None)
        if snapshot.operation_id is not None and counter is not None:
            writes = await counter(str(snapshot.operation_id))
        price, quantity, publication = await self._authoritative_state(spec)
        return _Outcome(
            tool_call_status=ToolCallStatus(snapshot.status),
            failure_code=snapshot.failure_code,
            authoritative_write_count=writes,
            authoritative_price=price,
            authoritative_quantity=quantity,
            authoritative_publication_state=publication,
        )

    async def confirm(self, run: Any) -> Any:
        assert run.status is RunStatus.WAITING_CONFIRMATION
        return await self._application.confirm(run.pending_confirmation_id)

    async def _authoritative_state(
        self, spec: _ToolSpec
    ) -> tuple[Decimal, int, ListingStatus]:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            listing = (
                await session.scalars(
                    sa.select(Listing).where(Listing.id == self.ids.listing_id)
                )
            ).one()
            level = (
                await session.scalars(
                    sa.select(InventoryLevel).where(
                        InventoryLevel.workspace_id == self.ids.workspace_id
                    )
                )
            ).one()
            return listing.current_price, level.available_quantity, listing.status
        finally:
            await session.close()

    async def load_operation(self, operation_id: uuid.UUID) -> Operation:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            return (
                await session.scalars(
                    sa.select(Operation).where(Operation.operation_id == operation_id)
                )
            ).one()
        finally:
            await session.close()

    async def replay_command(self, operation_id: uuid.UUID, spec: _ToolSpec) -> str:
        """按 expected_state 重建同 operation_id 命令并重放给 Adapter。"""
        operation = await self.load_operation(operation_id)
        expected = operation.expected_state
        assert expected is not None
        if spec.tool == "set_listing_price":
            receipt = await self._application.adapter.set_price(
                SetListingPriceCommand(
                    operation_id=str(operation_id),
                    idempotency_key=f"op:{operation_id}",
                    workspace_id=uuid.UUID(expected["workspace_id"]),
                    store_id=uuid.UUID(expected["store_id"]),
                    listing_id=uuid.UUID(expected["listing_id"]),
                    external_store_ref=expected["external_store_ref"],
                    external_product_ref=expected["external_product_ref"],
                    sku=expected["sku"],
                    target_price=Decimal(expected["target_price"]),
                    currency=expected["currency"],
                    expected_version=int(expected["expected_version"]),
                )
            )
        elif spec.tool == "adjust_inventory_quantity":
            receipt = await self._application.adapter.adjust_inventory(
                AdjustInventoryCommand(
                    operation_id=str(operation_id),
                    idempotency_key=derive_inventory_idempotency_key(
                        str(operation_id)
                    ),
                    workspace_id=uuid.UUID(expected["workspace_id"]),
                    store_id=uuid.UUID(expected["store_id"]),
                    location_id=uuid.UUID(expected["location_id"]),
                    product_id=uuid.UUID(expected["product_id"]),
                    external_store_ref=expected["external_store_ref"],
                    external_location_ref=expected["external_location_ref"],
                    external_product_ref=expected["external_product_ref"],
                    sku=expected["sku"],
                    delta=int(expected["delta"]),
                    reason=expected["reason"],
                    expected_version=int(expected["expected_version"]),
                    quantity_before=int(expected["quantity_before"]),
                )
            )
        else:
            direction = (
                self._application.adapter.publish_listing
                if spec.tool == "publish_listing"
                else self._application.adapter.unpublish_listing
            )
            receipt = await direction(
                ChangePublicationCommand(
                    operation_id=str(operation_id),
                    idempotency_key=derive_publication_idempotency_key(
                        str(operation_id)
                    ),
                    workspace_id=uuid.UUID(expected["workspace_id"]),
                    store_id=uuid.UUID(expected["store_id"]),
                    listing_id=uuid.UUID(expected["listing_id"]),
                    external_store_ref=expected["external_store_ref"],
                    external_product_ref=expected["external_product_ref"],
                    sku=expected["sku"],
                    reason=expected["reason"],
                    expected_version=int(expected["expected_version"]),
                )
            )
        return str(receipt.status.value)


@pytest.fixture()
async def matrix_runner(
    engine: AsyncEngine, product_application: ProductApplication
) -> MatrixRunner:
    return MatrixRunner(application=product_application, engine=engine)


def _denied(outcome: _Outcome, code: str) -> None:
    assert outcome.tool_call_status is ToolCallStatus.DENIED
    assert outcome.failure_code == code
    assert outcome.authoritative_write_count == 0


def _unchanged(outcome: _Outcome, spec: _ToolSpec) -> None:
    assert outcome.authoritative_price == BASE_PRICE
    assert outcome.authoritative_quantity == BASE_QUANTITY
    if spec.tool == "publish_listing":
        assert outcome.authoritative_publication_state is ListingStatus.UNPUBLISHED
    else:
        assert outcome.authoritative_publication_state is ListingStatus.PUBLISHED


def _changed(outcome: _Outcome, spec: _ToolSpec) -> None:
    if spec.tool == "set_listing_price":
        assert outcome.authoritative_price == TARGET_PRICE
        assert outcome.authoritative_quantity == BASE_QUANTITY
        assert outcome.authoritative_publication_state is ListingStatus.PUBLISHED
    elif spec.tool == "adjust_inventory_quantity":
        assert outcome.authoritative_price == BASE_PRICE
        assert outcome.authoritative_quantity == ADJUSTED_QUANTITY
        assert outcome.authoritative_publication_state is ListingStatus.PUBLISHED
    elif spec.tool == "publish_listing":
        assert outcome.authoritative_publication_state is ListingStatus.PUBLISHED
    else:
        assert outcome.authoritative_publication_state is ListingStatus.UNPUBLISHED


async def _run_scenario(
    runner: MatrixRunner, spec: _ToolSpec, scenario: str
) -> _Outcome:
    if scenario == "missing_write_permission":
        # 基线种子含 product.price.write;统一撤销该工具的写权限。
        await runner.revoke_write_permission(spec)
        run = await runner.start_write(spec, store_ref=DEFAULT_STORE_REF)
        return await runner.outcome(run.run_id, spec)
    if scenario == "out_of_scope_store":
        await runner.grant_write_permission(spec)
        await runner.seed_out_of_scope_store()
        await runner.restrict_store_scope(include_taobao=True)
        run = await runner.start_write(spec, store_ref=OUT_OF_SCOPE_STORE_REF)
        return await runner.outcome(run.run_id, spec)
    if scenario == "missing_scope_rows":
        await runner.grant_write_permission(spec)
        await runner.restrict_store_scope(include_taobao=False)
        run = await runner.start_write(spec, store_ref=DEFAULT_STORE_REF)
        return await runner.outcome(run.run_id, spec)
    if scenario == "version_changed_after_confirmation":
        await runner.grant_write_permission(spec)
        run = await runner.start_write(spec, store_ref=DEFAULT_STORE_REF)
        if spec.targets_inventory:
            await runner.bump_inventory_version()
        else:
            await runner.bump_listing_version()
        await runner.confirm(run)
        return await runner.outcome(run.run_id, spec)
    if scenario == "response_lost":
        await runner.grant_write_permission(spec)
        runner.application.adapter.faults.inject("response_lost_after_commit")
        run = await runner.start_write(spec, store_ref=DEFAULT_STORE_REF)
        run = await runner.confirm(run)
        assert run.status is RunStatus.WAITING_RECONCILIATION
        run = await runner.application.recover(run.run_id)
        return await runner.outcome(run.run_id, spec)
    if scenario == "duplicate_submit":
        await runner.grant_write_permission(spec)
        run = await runner.start_write(spec, store_ref=DEFAULT_STORE_REF)
        run = await runner.confirm(run)
        assert run.status is RunStatus.COMPLETED
        snapshot = await runner.application.tool_call_snapshot(run.run_id)
        assert snapshot is not None and snapshot.operation_id is not None
        replay_status = await runner.replay_command(
            uuid.UUID(str(snapshot.operation_id)), spec
        )
        assert replay_status == "APPLIED"
        # 重放后权威状态不变、逻辑写不增加。
        return await runner.outcome(run.run_id, spec)
    raise KeyError(scenario)


@pytest.mark.parametrize("spec", _TOOLS, ids=lambda spec: spec.tool)
@pytest.mark.parametrize(
    "scenario",
    [
        "missing_write_permission",
        "out_of_scope_store",
        "missing_scope_rows",
        "version_changed_after_confirmation",
        "response_lost",
        "duplicate_submit",
    ],
)
async def test_p1_write_tool_guard_matrix(
    matrix_runner: MatrixRunner, spec: _ToolSpec, scenario: str
) -> None:
    """四个写工具 × 六类场景:统一守卫语义 + 权威终态。"""
    outcome = await _run_scenario(matrix_runner, spec, scenario)
    if scenario == "missing_write_permission":
        _denied(outcome, "PERMISSION_DENIED")
        _unchanged(outcome, spec)
    elif scenario in ("out_of_scope_store", "missing_scope_rows"):
        _denied(outcome, "RESOURCE_SCOPE_DENIED")
        _unchanged(outcome, spec)
    elif scenario == "version_changed_after_confirmation":
        assert outcome.tool_call_status is spec.conflict_status
        assert outcome.failure_code == spec.conflict_code
        assert outcome.authoritative_write_count == 0
        _unchanged(outcome, spec)
    elif scenario == "response_lost":
        assert outcome.tool_call_status is ToolCallStatus.SUCCEEDED
        assert outcome.authoritative_write_count == 1
        _changed(outcome, spec)
    else:  # duplicate_submit
        assert outcome.tool_call_status is ToolCallStatus.SUCCEEDED
        assert outcome.authoritative_write_count == 1
        _changed(outcome, spec)


# ---------------------------------------------------------------------------
# Adapter 能力边界:REST Adapter(仅声明 pricing)上的库存/刊登写失败闭合
# ---------------------------------------------------------------------------


def _deny_all_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError("REST adapter must fail closed before any network call")


def _build_rest_adapter() -> RestCommerceAdapter:
    return RestCommerceAdapter(
        base_url="https://commerce.example.test/v1",
        allowed_hosts=frozenset({"commerce.example.test"}),
        access_token="p1-matrix-secret",
        client=httpx.Client(transport=httpx.MockTransport(_deny_all_network)),
    )


@pytest.fixture()
def commerce_adapter_for_application(request: pytest.FixtureRequest):
    """矩阵用 Adapter 注入点:indirect 参数为 "rest" 时注入 REST Adapter。"""
    if getattr(request, "param", None) == "rest":
        return _build_rest_adapter()
    return None


def _capability_specs() -> tuple[_ToolSpec, ...]:
    return tuple(spec for spec in _TOOLS if spec.tool != "set_listing_price")


@pytest.mark.parametrize("commerce_adapter_for_application", ["rest"], indirect=True)
@pytest.mark.parametrize("spec", _capability_specs(), ids=lambda spec: spec.tool)
async def test_p1_unsupported_capability_fails_closed(
    matrix_runner: MatrixRunner, spec: _ToolSpec
) -> None:
    """REST Adapter 只声明 pricing:库存/刊登写在到达网络前失败闭合。"""
    await matrix_runner.grant_write_permission(spec)
    run = await matrix_runner.start_write(spec, store_ref=DEFAULT_STORE_REF)
    if run.status is RunStatus.WAITING_CONFIRMATION:
        # 库存策略不声明能力边界:失败发生在确认后的执行期回执。
        run = await matrix_runner.confirm(run)
    outcome = await matrix_runner.outcome(run.run_id, spec)
    assert outcome.tool_call_status in (
        ToolCallStatus.DENIED,
        ToolCallStatus.FAILED,
    )
    assert outcome.failure_code == "CAPABILITY_UNSUPPORTED"
    assert outcome.authoritative_write_count == 0
    _unchanged(outcome, spec)
