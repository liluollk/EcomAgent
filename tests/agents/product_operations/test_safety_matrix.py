"""阶段 3 Gate:商品改价安全矩阵(计划 03 Task 7;架构文档 §5、§6、§7、§9)。

``safety_case_runner`` 夹具封装 ``product_application`` 组装(真库 + 确定性脚本
模型),按 case_id 布置场景环境(店铺范围、成员权限、活动锁价、Listing
版本、平台故障),驱动 start/confirm/recover/重复提交到终态,并统计:

- ``tool_call_status``:Run 内最新 ToolCall 的持久化状态;
- ``authoritative_write_count``:有 ``operation_id`` 时取平台的逻辑写计数
  (``MockCommerceAdapter.count_logical_writes``),否则为 0;
- ``authoritative_price``:权威 ``listings.current_price``。

矩阵 11 条(前 6 条为计划原文参数化,后 5 条补齐架构文档 §9 验收清单):

==============  ========================  ====  =========================
case_id         ToolCall 状态             写数  拦截点
missing_store   WAITING_INPUT             0     Schema 缺失字段 → Input
guessed_store   WAITING_INPUT             0     Provenance UNTRUSTED → Input
out_of_scope_store  DENIED                0     Access Control RESOURCE_SCOPE_DENIED
below_floor_price   DENIED                0     PricePolicy PRICE_BELOW_FLOOR
response_lost   SUCCEEDED                 1     recover 回查 APPLIED,仅一次写
duplicate_submit    SUCCEEDED             1     平台幂等回放,逻辑写不增加
missing_write_permission  DENIED           0     Access Control PERMISSION_DENIED
read_only_tool  SUCCEEDED                 0     READ Tool 执行,无权威写
active_price_lock   DENIED                0     PricePolicy PRICE_LOCKED
permission_revoked_after_confirmation    DENIED  0  确认后重评权限已撤销
listing_version_changed_after_confirmation DENIED 0  确认后 Listing 版本已变化
==============  ========================  ====  =========================

read_only_tool 实际为 SUCCEEDED + 0 写:矩阵允许"DENIED 或不产生写",
实际走 READ_ONLY 执行模式,证明 READ effect 不产生权威写。

Gate 驱动的最小实现修复(详见设计文档"实现验证状态"小节):

1. 不可信店铺/SKU 参数 → Input:组合根 PreToolUse 管线在
   argument_provenance 阶段后加证据守卫(apps/worker/assembly.py),把
   "已提供但来源不可信"的 store_id/sku 并入校验诊断,由既有
   input_requirement 阶段转成 Input Request;guessed_store 因此得到
   WAITING_INPUT 而不是继续进入授权与执行阶段。
2. Workspace 别名表证据:店铺证据匹配候选扩大为当前 Workspace 全部
   ACTIVE 店铺(agents/product_operations/input/requirements.py,架构文档
   §5"通过 Workspace 别名表确定性归一化");越权店铺的用户显式证据得以
   进入 Access Control 并被 RESOURCE_SCOPE_DENIED 拒绝,Input Request
   候选项仍严格限定授权范围。
3. 确认后 Listing Version 绑定:组合根在首次通过价格 Policy 时绑定
   Listing 版本,确认后重评时比对,变化即 LISTING_VERSION_CONFLICT
   (架构文档 §6 第 7 条)。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.persistence.models import Listing, PriceLock, Store
from agents.product_operations.pricing.gateway import SetListingPriceCommand
from apps.assembly import ProductApplication, RunView
from runtime.backend.protocol import BackendCompleted, ToolCallCompleted
from runtime.interfaces.state import RunStatus
from runtime.persistence.base import ResourceScopeMode, ToolCallStatus
from runtime.persistence.models.execution import Operation
from runtime.persistence.models.identity import (
    MembershipPermission,
    MembershipResourceScope,
    MembershipResourceScopeMode,
)


BASE_PRICE = Decimal("128.00")
TARGET_PRICE = Decimal("99.00")
WRITE_PERMISSION = "product.price.write"


@dataclass(frozen=True)
class _CaseSpec:
    """每个场景的权威预期:ToolCall 状态、逻辑写数、最终价格、失败码。"""

    tool_call_status: ToolCallStatus
    authoritative_write_count: int
    authoritative_price: Decimal
    failure_code: str | None


_CASES: Mapping[str, _CaseSpec] = {
    "missing_store": _CaseSpec(ToolCallStatus.WAITING_INPUT, 0, BASE_PRICE, None),
    "guessed_store": _CaseSpec(ToolCallStatus.WAITING_INPUT, 0, BASE_PRICE, None),
    "out_of_scope_store": _CaseSpec(
        ToolCallStatus.DENIED, 0, BASE_PRICE, "RESOURCE_SCOPE_DENIED"
    ),
    "below_floor_price": _CaseSpec(
        ToolCallStatus.DENIED, 0, BASE_PRICE, "PRICE_BELOW_FLOOR"
    ),
    # response_lost:UNKNOWN 期间写入的失败码在回查成功后保留(实际实现);
    # 状态已 SUCCEEDED、权威写恰 1 次,失败码只是恢复前的历史痕迹。
    "response_lost": _CaseSpec(
        ToolCallStatus.SUCCEEDED, 1, TARGET_PRICE, "RESPONSE_LOST_AFTER_COMMIT"
    ),
    "duplicate_submit": _CaseSpec(ToolCallStatus.SUCCEEDED, 1, TARGET_PRICE, None),
    "missing_write_permission": _CaseSpec(
        ToolCallStatus.DENIED, 0, BASE_PRICE, "PERMISSION_DENIED"
    ),
    "read_only_tool": _CaseSpec(ToolCallStatus.SUCCEEDED, 0, BASE_PRICE, None),
    "active_price_lock": _CaseSpec(ToolCallStatus.DENIED, 0, BASE_PRICE, "PRICE_LOCKED"),
    "permission_revoked_after_confirmation": _CaseSpec(
        ToolCallStatus.DENIED, 0, BASE_PRICE, "PERMISSION_DENIED"
    ),
    "listing_version_changed_after_confirmation": _CaseSpec(
        ToolCallStatus.DENIED, 0, BASE_PRICE, "LISTING_VERSION_CONFLICT"
    ),
}


class SafetyCaseRunner:
    """安全矩阵驱动器:布置场景 → 执行闭环 → 统计权威结果。"""

    def __init__(
        self, *, application: ProductApplication, engine: AsyncEngine
    ) -> None:
        self._application = application
        self._engine = engine

    @property
    def application(self) -> ProductApplication:
        return self._application

    async def run(self, case_id: str) -> SafetyCaseResult:
        driver = _CASE_DRIVERS.get(case_id)
        if driver is None:
            raise KeyError(f"unknown safety case: {case_id}")
        run = await driver(self)
        application = self._application
        snapshot = await application.tool_call_snapshot(run.run_id)
        assert snapshot is not None, f"case {case_id} produced no tool call"
        writes = 0
        if snapshot.operation_id is not None:
            writes = await application.adapter.count_logical_writes(
                str(snapshot.operation_id)
            )
        spec = _CASES[case_id]
        return SafetyCaseResult(
            case_id=case_id,
            run_status=run.status,
            tool_call_status=ToolCallStatus(snapshot.status),
            failure_code=snapshot.failure_code,
            authoritative_write_count=writes,
            authoritative_price=await application.authoritative_price(
                str(application.seed_ids.store_id), "SKU-001"
            ),
            expected_price=spec.authoritative_price,
        )

    # -- 场景环境布置(独立短事务,立即提交) ----------------------------

    async def _mutate(self, action: Callable[[AsyncSession], Awaitable[None]]) -> None:
        session = AsyncSession(self._engine, expire_on_commit=False)
        try:
            async with session.begin():
                await action(session)
        finally:
            await session.close()

    async def seed_out_of_scope_store(self) -> str:
        """在工作区补一家抖音旗舰店(不在成员资源范围内,可被证据匹配)。"""
        ids = self._application.seed_ids
        store = Store(
            id=uuid.uuid4(),
            workspace_id=ids.workspace_id,
            platform=StorePlatform.DOUYIN,
            external_store_ref=f"douyin-{uuid.uuid4()}",
            name="抖音旗舰店",
            aliases=["抖音旗舰店"],
        )

        async def action(session: AsyncSession) -> None:
            session.add(store)

        await self._mutate(action)
        return str(store.id)

    async def restrict_store_scope_to_default(self) -> None:
        """把成员的 store 范围从 ALL 收窄为 SELECTED(仅默认淘宝店)。"""
        ids = self._application.seed_ids

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
            session.add(
                MembershipResourceScope(
                    workspace_id=ids.workspace_id,
                    membership_id=ids.membership_id,
                    resource_type="store",
                    resource_id=str(ids.store_id),
                )
            )

        await self._mutate(action)

    async def revoke_write_permission(self) -> None:
        """撤销成员的 product.price.write 权限。"""
        ids = self._application.seed_ids

        async def action(session: AsyncSession) -> None:
            await session.execute(
                sa.delete(MembershipPermission).where(
                    MembershipPermission.workspace_id == ids.workspace_id,
                    MembershipPermission.membership_id == ids.membership_id,
                    MembershipPermission.permission == WRITE_PERMISSION,
                )
            )

        await self._mutate(action)

    async def add_active_price_lock(self) -> None:
        """给默认 Listing 加一个当前生效的活动锁价。"""
        ids = self._application.seed_ids
        now = datetime.now(tz=UTC)
        lock = PriceLock(
            id=uuid.uuid4(),
            workspace_id=ids.workspace_id,
            listing_id=ids.listing_id,
            reason="safety-matrix-active-lock",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=7),
        )

        async def action(session: AsyncSession) -> None:
            session.add(lock)

        await self._mutate(action)

    async def bump_listing_version(self) -> None:
        """并发修改默认 Listing(版本 +1),模拟确认后的平台侧变更。"""
        ids = self._application.seed_ids

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

    # -- 驱动入口 ---------------------------------------------------------

    async def start_update_price(
        self, *, store_ref: str | None, message: str, price: int
    ) -> RunView:
        """脚本化一轮 set_listing_price ToolCall 并发起 Run。"""
        self._application.scripted_backend.set_rounds(
            [
                [
                    _set_price_call(store_ref, "SKU-001", price),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        )
        return await self._application.start(message)

    async def start_read_snapshot(self, *, store_ref: str, message: str) -> RunView:
        """脚本化一轮 get_listing ToolCall(READ Tool)并发起 Run。"""
        self._application.scripted_backend.set_rounds(
            [
                [
                    ToolCallCompleted(
                        call_id="c0",
                        name="get_listing",
                        arguments={"store_ref": store_ref, "sku": "SKU-001"},
                    ),
                    BackendCompleted(finish_reason="tool_calls"),
                ]
            ]
        )
        return await self._application.start(message)


@dataclass(frozen=True)
class SafetyCaseResult:
    """单个安全场景的执行结果与权威统计。"""

    case_id: str
    run_status: RunStatus
    tool_call_status: ToolCallStatus
    failure_code: str | None
    authoritative_write_count: int
    authoritative_price: Decimal
    expected_price: Decimal


# ---- 场景驱动(布置 + 执行) ------------------------------------------


def _set_price_call(
    store_ref: str | None, sku: str, price: int
) -> ToolCallCompleted:
    """模型提出 set_listing_price；store_ref 为 None 表示缺失该参数。"""
    arguments: dict[str, object] = {"sku": sku, "target_price": price}
    if store_ref is not None:
        arguments["store_ref"] = store_ref
    return ToolCallCompleted(
        call_id="c0", name="set_listing_price", arguments=arguments
    )


async def _drive_missing_store(runner: SafetyCaseRunner) -> RunView:
    return await runner.start_update_price(
        store_ref=None, message="把 SKU-001 改成 99 元", price=99
    )


async def _drive_guessed_store(runner: SafetyCaseRunner) -> RunView:
    """模型编造无证据店铺:消息无店铺证据,参数携带不存在的店铺 ID。"""
    return await runner.start_update_price(
        store_ref=str(uuid.uuid4()), message="把 SKU-001 改成 99 元", price=99
    )


async def _drive_out_of_scope_store(runner: SafetyCaseRunner) -> RunView:
    """用户显式给出越权店铺:范围收窄后,消息与参数均指向抖音旗舰店。"""
    await runner.seed_out_of_scope_store()
    await runner.restrict_store_scope_to_default()
    return await runner.start_update_price(
        store_ref="抖音旗舰店", message="把抖音旗舰店 SKU-001 改成 99 元", price=99
    )


async def _drive_below_floor_price(runner: SafetyCaseRunner) -> RunView:
    return await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 30 元",
        price=30,
    )


async def _drive_response_lost(runner: SafetyCaseRunner) -> RunView:
    runner.application.adapter.faults.inject("response_lost_after_commit")
    run = await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 99 元",
        price=99,
    )
    assert run.status is RunStatus.WAITING_CONFIRMATION
    run = await runner.application.confirm(run.pending_confirmation_id)  # type: ignore[arg-type]
    # 进程内执行器在 confirm 内就完成回查(RECONCILE 后立即续推),因此返回时
    # Run 已离开 WAITING_RECONCILIATION;持久化的不确定状态仍在上一步提交过,
    # 崩溃窗口由恢复扫描覆盖。
    assert run.status is not RunStatus.WAITING_RECONCILIATION
    return await runner.application.recover(run.run_id)


async def _drive_duplicate_submit(runner: SafetyCaseRunner) -> RunView:
    run = await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 99 元",
        price=99,
    )
    assert run.status is RunStatus.WAITING_CONFIRMATION
    run = await runner.application.confirm(run.pending_confirmation_id)  # type: ignore[arg-type]
    assert run.status is RunStatus.COMPLETED
    snapshot = await runner.application.tool_call_snapshot(run.run_id)
    assert snapshot is not None
    operation_id = snapshot.operation_id
    assert operation_id is not None
    operation = await _load_operation(runner.application._engine, operation_id)
    expected = operation.expected_state
    assert expected is not None
    replay = await runner.application.adapter.set_price(
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
    assert replay.status.value == "APPLIED"
    return run


async def _drive_missing_write_permission(runner: SafetyCaseRunner) -> RunView:
    await runner.revoke_write_permission()
    return await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 99 元",
        price=99,
    )


async def _drive_read_only_tool(runner: SafetyCaseRunner) -> RunView:
    """READ Tool 全链路执行:不需要确认,也不产生任何权威写。"""
    return await runner.start_read_snapshot(
        store_ref="淘宝旗舰店",
        message="看一下淘宝旗舰店 SKU-001 现在的售价",
    )


async def _drive_active_price_lock(runner: SafetyCaseRunner) -> RunView:
    await runner.add_active_price_lock()
    return await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 99 元",
        price=99,
    )


async def _drive_permission_revoked_after_confirmation(
    runner: SafetyCaseRunner,
) -> RunView:
    run = await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 99 元",
        price=99,
    )
    assert run.status is RunStatus.WAITING_CONFIRMATION
    await runner.revoke_write_permission()
    return await runner.application.confirm(run.pending_confirmation_id)  # type: ignore[arg-type]


async def _drive_listing_version_changed_after_confirmation(
    runner: SafetyCaseRunner,
) -> RunView:
    run = await runner.start_update_price(
        store_ref="淘宝旗舰店",
        message="把淘宝旗舰店 SKU-001 改成 99 元",
        price=99,
    )
    assert run.status is RunStatus.WAITING_CONFIRMATION
    await runner.bump_listing_version()
    return await runner.application.confirm(run.pending_confirmation_id)  # type: ignore[arg-type]


_CASE_DRIVERS: Mapping[str, Callable[[SafetyCaseRunner], Awaitable[RunView]]] = {
    "missing_store": _drive_missing_store,
    "guessed_store": _drive_guessed_store,
    "out_of_scope_store": _drive_out_of_scope_store,
    "below_floor_price": _drive_below_floor_price,
    "response_lost": _drive_response_lost,
    "duplicate_submit": _drive_duplicate_submit,
    "missing_write_permission": _drive_missing_write_permission,
    "read_only_tool": _drive_read_only_tool,
    "active_price_lock": _drive_active_price_lock,
    "permission_revoked_after_confirmation": _drive_permission_revoked_after_confirmation,
    "listing_version_changed_after_confirmation": _drive_listing_version_changed_after_confirmation,
}


async def _load_operation(engine: AsyncEngine, operation_id: uuid.UUID) -> Operation:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        return (
            await session.scalars(
                sa.select(Operation).where(Operation.operation_id == operation_id)
            )
        ).one()
    finally:
        await session.close()


# ---- 夹具 --------------------------------------------------------------


@pytest.fixture()
async def safety_case_runner(
    engine: AsyncEngine, product_application: ProductApplication
) -> SafetyCaseRunner:
    return SafetyCaseRunner(application=product_application, engine=engine)


# ---- 计划原文参数化(6 条) --------------------------------------------


@pytest.mark.parametrize(
    ("case_id", "expected_status", "expected_writes"),
    [
        ("missing_store", ToolCallStatus.WAITING_INPUT, 0),
        ("guessed_store", ToolCallStatus.WAITING_INPUT, 0),
        ("out_of_scope_store", ToolCallStatus.DENIED, 0),
        ("below_floor_price", ToolCallStatus.DENIED, 0),
        ("response_lost", ToolCallStatus.SUCCEEDED, 1),
        ("duplicate_submit", ToolCallStatus.SUCCEEDED, 1),
    ],
)
async def test_price_safety_matrix(
    safety_case_runner: SafetyCaseRunner,
    case_id: str,
    expected_status: ToolCallStatus,
    expected_writes: int,
) -> None:
    """计划原文矩阵:每条同时断言 ToolCall 状态与权威写入次数。"""
    result = await safety_case_runner.run(case_id)
    assert result.tool_call_status is expected_status
    assert result.authoritative_write_count == expected_writes


# ---- 补充场景参数化(5 条;架构文档 §9 验收清单) ----------------------


@pytest.mark.parametrize(
    ("case_id", "expected_status", "expected_writes"),
    [
        ("missing_write_permission", ToolCallStatus.DENIED, 0),
        ("read_only_tool", ToolCallStatus.SUCCEEDED, 0),
        ("active_price_lock", ToolCallStatus.DENIED, 0),
        ("permission_revoked_after_confirmation", ToolCallStatus.DENIED, 0),
        ("listing_version_changed_after_confirmation", ToolCallStatus.DENIED, 0),
    ],
)
async def test_price_safety_matrix_extended(
    safety_case_runner: SafetyCaseRunner,
    case_id: str,
    expected_status: ToolCallStatus,
    expected_writes: int,
) -> None:
    """补充矩阵:缺写权限、READ Tool、活动锁价、确认后权限撤销与版本变化。"""
    result = await safety_case_runner.run(case_id)
    assert result.tool_call_status is expected_status
    assert result.authoritative_write_count == expected_writes


# ---- 权威最终状态与失败码(全部 11 条逐项核对) ------------------------


@pytest.mark.parametrize("case_id", sorted(_CASES))
async def test_safety_matrix_authoritative_final_state(
    safety_case_runner: SafetyCaseRunner, case_id: str
) -> None:
    """每条场景的权威价格与失败码必须与矩阵登记一致。"""
    spec = _CASES[case_id]
    result = await safety_case_runner.run(case_id)
    assert result.authoritative_price == spec.authoritative_price
    assert result.failure_code == spec.failure_code
