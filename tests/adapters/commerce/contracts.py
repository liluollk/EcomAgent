"""Commerce Adapter 的**能力契约**测试套件(计划 P1 Task 9)。

同一个 Gateway Port 可以有多份实现:Mock 以业务表为平台权威状态,REST 以固定
HTTP 协议为平台权威状态。平台差异不该让契约漂移,因此把"一个 Port 实现必须满足的
行为"抽成可复用协程,由各 Adapter 的测试分别调用:

- :func:`assert_price_gateway_contract` 对应 ``PricingGateway``;
- :func:`assert_inventory_gateway_contract` 对应 ``InventoryGateway``;
- :func:`assert_listing_gateway_contract` 对应 ``ListingGateway``。

每个协程按同一顺序跑五步,每步都同时断言**回执**、**平台权威状态**与**逻辑写次数**:

1. 成功写入:``APPLIED`` + 观测齐全,权威状态就地推进一个版本,逻辑写 1 次;
2. 旧版本冲突:CAS 拒绝 → 确定 ``FAILED`` + ``VERSION_CONFLICT``,零逻辑写,
   权威状态不变(确定失败绝不退化成 ``UNKNOWN``,也绝不带观测);
3. 同 ``operation_id`` 重复提交:按平台幂等契约回放**同一**回执,不产生第二次逻辑写;
4. 写后丢响应:调用方只拿到**无观测**的 ``UNKNOWN``,但写入已生效(逻辑写 1 次,
   权威状态已改变);
5. 按该 ``operation_id`` 回查:确认 ``APPLIED`` 且回查本身不产生写入;从未见过的
   ``operation_id`` 确定为 ``NOT_APPLIED``——不得因"资源此刻恰好如此"而判 APPLIED。

契约只依赖下面的 ``*ContractEnv``:命令怎么构造、权威状态怎么读、逻辑写怎么数、
响应怎么丢,全部由测试侧提供。因此 DB Mock 与 HTTP REST 可以复用同一套断言,而
契约本身不 import 任何具体 Adapter。

注意:``logical_writes`` 是**平台侧写路径实际执行次数**的观测入口(测试探针),
不是 Port 契约的一部分;真实平台只能用同口径的"请求次数/写入条数"来提供它。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal

from adapters.commerce.protocol import REASON_VERSION_CONFLICT
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.inventory.gateway import (
    AdjustInventoryCommand,
    InventoryGateway,
    ReconcileInventoryCommand,
)
from agents.product_operations.listing.gateway import (
    ListingGateway,
    ReconcileListingCommand,
)
from agents.product_operations.pricing.gateway import (
    PricingGateway,
    ReconcilePriceCommand,
    SetListingPriceCommand,
)
from agents.product_operations.shared.write_contracts import WriteReceipt, WriteStatus
from runtime.execution.reconciliation import ReconcileResult


@dataclass(frozen=True)
class PriceContractEnv:
    """价格契约运行所需的一切:Port、命令构造与观测入口。"""

    gateway: PricingGateway
    build_write: Callable[[str, int, Decimal], SetListingPriceCommand]
    """``(operation_id, expected_version, target_price) -> 写命令``。"""
    build_reconcile: Callable[[str, Decimal], ReconcilePriceCommand]
    """``(operation_id, expected_price) -> 回查命令``。"""
    read_state: Callable[[], Awaitable[tuple[Decimal, int]]]
    """读**平台权威**价格与版本:``() -> (price, listing_version)``。"""
    logical_writes: Callable[[str], Awaitable[int]]
    """``operation_id -> 平台侧已发生的逻辑写次数``。"""
    lose_response: Callable[[], AbstractContextManager[None]]
    """进入时令"写入已提交但响应丢失",退出时复原。"""
    initial_price: Decimal
    target_price: Decimal
    second_target_price: Decimal
    initial_version: int


@dataclass(frozen=True)
class InventoryContractEnv:
    """库存契约运行所需的一切:Port、命令构造与观测入口。"""

    gateway: InventoryGateway
    build_write: Callable[[str, int, int, int], AdjustInventoryCommand]
    """``(operation_id, expected_version, delta, quantity_before) -> 写命令``。

    ``quantity_before`` 与 ``expected_version`` 必须来自**同一次**写前读取:
    两者共同构成"这次操作基于哪份状态",也共同决定 ``expected_quantity_after``。
    """
    build_reconcile: Callable[[str, int, int], ReconcileInventoryCommand]
    """``(operation_id, quantity_before, expected_quantity_after) -> 回查命令``。"""
    read_state: Callable[[], Awaitable[tuple[int, int]]]
    """读**平台权威**数量与版本:``() -> (available_quantity, inventory_version)``。"""
    logical_writes: Callable[[str], Awaitable[int]]
    lose_response: Callable[[], AbstractContextManager[None]]
    initial_quantity: int
    first_delta: int
    second_delta: int
    initial_version: int


@dataclass(frozen=True)
class ListingContractEnv:
    """刊登契约运行所需的一切:Port、命令构造与观测入口。

    ``build_change`` 把"目标发布状态"映射到具体的 Port 方法(``publish_listing`` /
    ``unpublish_listing``)——方向由 Port 方法承载,命令本身不带目标状态。
    """

    gateway: ListingGateway
    build_change: Callable[[ListingStatus, str, int], Awaitable[WriteReceipt]]
    """``(target_state, operation_id, expected_version) -> 写回执``。"""
    build_reconcile: Callable[[str, ListingStatus], ReconcileListingCommand]
    """``(operation_id, expected_state) -> 回查命令``。"""
    read_state: Callable[[], Awaitable[tuple[ListingStatus, int]]]
    """读**平台权威**发布状态与版本。"""
    logical_writes: Callable[[str], Awaitable[int]]
    lose_response: Callable[[], AbstractContextManager[None]]
    initial_state: ListingStatus
    initial_version: int


async def assert_price_gateway_contract(env: PriceContractEnv) -> None:
    """价格 Port 契约:成功写入 → 版本冲突 → 重复提交 → 丢响应 → 按 operation_id 回查。"""
    gateway = env.gateway
    applied_id = "contract-price-applied"
    conflict_id = "contract-price-conflict"
    lost_id = "contract-price-response-lost"

    # 写前基线:后续所有版本断言都由这一次读取推导。
    state = await env.read_state()
    assert state == (env.initial_price, env.initial_version), state
    version = state[1]
    after_first = (env.target_price, version + 1)
    after_second = (env.second_target_price, version + 2)

    # 1. 成功写入:回执带观测,权威状态就地推进一个版本。
    applied = await gateway.set_price(
        env.build_write(applied_id, version, env.target_price)
    )
    assert applied.status is WriteStatus.APPLIED, applied
    assert applied.changed is True
    assert applied.before_price == env.initial_price
    assert applied.after_price == env.target_price
    assert applied.resource_version == version + 1
    assert await env.read_state() == after_first
    assert await env.logical_writes(applied_id) == 1

    # 2. 旧版本冲突:确定失败、零逻辑写、权威状态不变,且回执不得带观测。
    conflict = await gateway.set_price(
        env.build_write(conflict_id, version, env.second_target_price)
    )
    assert conflict.status is WriteStatus.FAILED, conflict
    assert conflict.reason_code == REASON_VERSION_CONFLICT
    assert conflict.after_price is None
    assert conflict.resource_version is None
    assert await env.logical_writes(conflict_id) == 0
    assert await env.read_state() == after_first

    # 3. 重复提交**同一条**已冻结的命令(与两阶段协议重试 execute_prepared 一致):
    #    按平台幂等契约回放同一回执,不产生第二次逻辑写。
    replay = await gateway.set_price(
        env.build_write(applied_id, version, env.target_price)
    )
    assert replay == applied
    assert await env.logical_writes(applied_id) == 1
    assert await env.read_state() == after_first

    # 4. 写后丢响应:写入已生效,调用方只能拿到无观测的 UNKNOWN。
    with env.lose_response():
        lost = await gateway.set_price(
            env.build_write(lost_id, version + 1, env.second_target_price)
        )
    assert lost.status is WriteStatus.UNKNOWN, lost
    assert lost.after_price is None
    assert lost.resource_version is None
    assert lost.changed is None
    assert await env.logical_writes(lost_id) == 1
    assert await env.read_state() == after_second

    # 5. 按该 operation_id 回查:确认 APPLIED,且回查不产生新的写入。
    assert (
        await gateway.reconcile_price(
            env.build_reconcile(lost_id, env.second_target_price)
        )
        is ReconcileResult.APPLIED
    )
    assert await env.logical_writes(lost_id) == 1
    assert await env.read_state() == after_second
    # 从未见过的 operation_id:即使资源此刻恰好是期望价,也不得判 APPLIED。
    assert (
        await gateway.reconcile_price(
            env.build_reconcile("contract-price-never-seen", env.second_target_price)
        )
        is ReconcileResult.NOT_APPLIED
    )


async def assert_inventory_gateway_contract(env: InventoryContractEnv) -> None:
    """库存 Port 契约:成功调整 → 版本冲突 → 重复提交 → 丢响应 → 按 operation_id 回查。"""
    gateway = env.gateway
    applied_id = "contract-inventory-applied"
    conflict_id = "contract-inventory-conflict"
    lost_id = "contract-inventory-response-lost"

    # 写前基线:数量与版本来自同一次读取,后续断言全部由此推导。
    state = await env.read_state()
    assert state == (env.initial_quantity, env.initial_version), state
    quantity, version = state
    after_first_quantity = quantity + env.first_delta
    after_second_quantity = after_first_quantity + env.second_delta
    after_first = (after_first_quantity, version + 1)
    after_second = (after_second_quantity, version + 2)

    # 1. 成功调整:回执带观测,权威数量与版本各推进一次。
    applied = await gateway.adjust_inventory(
        env.build_write(applied_id, version, env.first_delta, quantity)
    )
    assert applied.status is WriteStatus.APPLIED, applied
    assert applied.changed is True
    assert applied.before_quantity == quantity
    assert applied.after_quantity == after_first_quantity
    assert applied.resource_version == version + 1
    assert await env.read_state() == after_first
    assert await env.logical_writes(applied_id) == 1

    # 2. 旧版本冲突:确定失败、零逻辑写、权威状态不变,且回执不得带观测。
    conflict = await gateway.adjust_inventory(
        env.build_write(conflict_id, version, env.second_delta, quantity)
    )
    assert conflict.status is WriteStatus.FAILED, conflict
    assert conflict.reason_code == REASON_VERSION_CONFLICT
    assert conflict.after_quantity is None
    assert conflict.resource_version is None
    assert await env.logical_writes(conflict_id) == 0
    assert await env.read_state() == after_first

    # 3. 重复提交**同一条**已冻结的命令(与两阶段协议重试 execute_prepared 一致):
    #    写前读取的数量与版本同样保持不变,回放同一回执,不产生第二次逻辑写。
    replay = await gateway.adjust_inventory(
        env.build_write(applied_id, version, env.first_delta, quantity)
    )
    assert replay == applied
    assert await env.logical_writes(applied_id) == 1
    assert await env.read_state() == after_first

    # 4. 写后丢响应:写入已生效,调用方只能拿到无观测的 UNKNOWN。
    with env.lose_response():
        lost = await gateway.adjust_inventory(
            env.build_write(
                lost_id, version + 1, env.second_delta, after_first_quantity
            )
        )
    assert lost.status is WriteStatus.UNKNOWN, lost
    assert lost.after_quantity is None
    assert lost.resource_version is None
    assert lost.changed is None
    assert await env.logical_writes(lost_id) == 1
    assert await env.read_state() == after_second

    # 5. 按该 operation_id 回查:确认 APPLIED,且回查不产生新的写入。
    assert (
        await gateway.reconcile_inventory(
            env.build_reconcile(lost_id, after_first_quantity, after_second_quantity)
        )
        is ReconcileResult.APPLIED
    )
    assert await env.logical_writes(lost_id) == 1
    assert await env.read_state() == after_second
    # 从未见过的 operation_id:即使数量此刻恰好等于预期,也不得判 APPLIED。
    assert (
        await gateway.reconcile_inventory(
            env.build_reconcile(
                "contract-inventory-never-seen",
                after_first_quantity,
                after_second_quantity,
            )
        )
        is ReconcileResult.NOT_APPLIED
    )


async def assert_listing_gateway_contract(env: ListingContractEnv) -> None:
    """刊登 Port 契约:成功变更 → 版本冲突 → 重复提交 → 丢响应 → 按 operation_id 回查。

    每一步都在两个发布状态之间**来回**切换:同目标写入是幂等 no-op(不推进版本),
    只有真正的状态迁移才能被"版本 +1"和"零逻辑写"这类断言观察到。
    """
    gateway = env.gateway
    applied_id = "contract-listing-applied"
    conflict_id = "contract-listing-conflict"
    lost_id = "contract-listing-response-lost"
    first_target = (
        ListingStatus.UNPUBLISHED
        if env.initial_state is ListingStatus.PUBLISHED
        else ListingStatus.PUBLISHED
    )

    # 写前基线:状态与版本由同一次读取确定。
    state = await env.read_state()
    assert state == (env.initial_state, env.initial_version), state
    version = state[1]
    after_first = (first_target, version + 1)
    after_second = (env.initial_state, version + 2)

    # 1. 成功变更:回执带观测且 changed=True,权威状态与版本各推进一次。
    applied = await env.build_change(first_target, applied_id, version)
    assert applied.status is WriteStatus.APPLIED, applied
    assert applied.changed is True
    assert applied.resource_version == version + 1
    assert await env.read_state() == after_first
    assert await env.logical_writes(applied_id) == 1

    # 2. 旧版本冲突:方向必须回到初始状态,否则会撞上"同目标 no-op"而不是 CAS。
    conflict = await env.build_change(env.initial_state, conflict_id, version)
    assert conflict.status is WriteStatus.FAILED, conflict
    assert conflict.reason_code == REASON_VERSION_CONFLICT
    assert conflict.resource_version is None
    assert await env.logical_writes(conflict_id) == 0
    assert await env.read_state() == after_first

    # 3. 重复提交**同一条**已冻结的命令(与两阶段协议重试 execute_prepared 一致):
    #    幂等去重先于"同目标 no-op",因此回放的是首次 APPLIED + changed=True 的回执。
    replay = await env.build_change(first_target, applied_id, version)
    assert replay == applied
    assert await env.logical_writes(applied_id) == 1
    assert await env.read_state() == after_first

    # 4. 写后丢响应:写入已生效,调用方只能拿到无观测的 UNKNOWN。
    with env.lose_response():
        lost = await env.build_change(env.initial_state, lost_id, version + 1)
    assert lost.status is WriteStatus.UNKNOWN, lost
    assert lost.resource_version is None
    assert lost.changed is None
    assert await env.logical_writes(lost_id) == 1
    assert await env.read_state() == after_second

    # 5. 按该 operation_id 回查:确认 APPLIED,且回查不产生新的写入。
    assert (
        await gateway.reconcile_listing(
            env.build_reconcile(lost_id, env.initial_state)
        )
        is ReconcileResult.APPLIED
    )
    assert await env.logical_writes(lost_id) == 1
    assert await env.read_state() == after_second
    # 从未见过的 operation_id:即使当前状态恰好等于期望状态,也不得判 APPLIED。
    assert (
        await gateway.reconcile_listing(
            env.build_reconcile("contract-listing-never-seen", env.initial_state)
        )
        is ReconcileResult.NOT_APPLIED
    )


__all__ = [
    "InventoryContractEnv",
    "ListingContractEnv",
    "PriceContractEnv",
    "assert_inventory_gateway_contract",
    "assert_listing_gateway_contract",
    "assert_price_gateway_contract",
]
