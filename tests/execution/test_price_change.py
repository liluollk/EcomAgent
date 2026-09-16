"""PriceChangeCoordinator 状态机与恢复测试（全程离线，使用假 PricePlatform）。"""

from decimal import Decimal

import pytest

from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceError,
    PriceErrorCode,
    PricePlatform,
    PriceWriteReceipt,
    PriceVerification,
    ProductRef,
    ProductSnapshot,
)
from execution.price_change import (
    IllegalStateTransition,
    PriceChangeCoordinator,
    PriceChangeOperation,
    PriceChangeState,
    assert_transition,
)
from session.operation_store import OperationStore


# ---------------------------------------------------------------------------
# 假平台与假成本价提供方
# ---------------------------------------------------------------------------
class FakeCostProvider:
    def __init__(self, cost: float | None) -> None:
        self.cost = cost

    def get_cost_price(self, channel, sku):
        return self.cost


class FakePlatform:
    """可脚本化的 PricePlatform 实现，确定性、无 HTTP。

    write_behavior:
      success            一次写入成功
      transient_then_ok  第一次写入瞬态错误，第二次成功（同键重试）
      unknown            写入超时且 side_effect_possible（进入未知结果）
      business           业务错误（不可重试）
    """

    def __init__(self, *, platform="mock", write_behavior="success",
                 verify_consistent=True, cost=Decimal("50")):
        self.platform = platform
        self.write_behavior = write_behavior
        self.verify_consistent = verify_consistent
        self.apply_calls = 0
        self.side_effects = 0
        self.seen_keys: set = set()
        self.applied_keys: list = []
        self._transient_count = 0
        self._cost = cost

    async def query_snapshot(self, ref: ProductRef) -> ProductSnapshot:
        return ProductSnapshot(
            product_ref=ref,
            name="sku",
            current_price=self._cost,
            stock=10,
            status="on_sale",
            activity_locked=False,
        )

    async def apply_price(self, command: PriceChangeCommand, *, idempotency_key=None):
        self.apply_calls += 1
        self.applied_keys.append(idempotency_key)
        if self.write_behavior == "unknown":
            # 写入超时且可能已落库：标记键已应用（供回放检测），计入一次副作用，再抛错
            self.seen_keys.add(idempotency_key)
            self.side_effects += 1
            raise PriceError(
                PriceErrorCode.UNKNOWN_OUTCOME,
                "写入超时，结果未知",
                side_effect_possible=True,
            )
        if self.write_behavior == "transient_then_ok":
            if self._transient_count < 1:
                self._transient_count += 1
                # 瞬态失败：平台未应用，不标记键、不计入副作用
                raise PriceError(PriceErrorCode.TRANSIENT_ERROR, "限流，稍后重试")
            replay = idempotency_key in self.seen_keys
            self.seen_keys.add(idempotency_key)
            if not replay:
                self.side_effects += 1
            return PriceWriteReceipt(
                product_ref=command.product_ref,
                applied_price=command.target_price,
                idempotent_replay=replay,
                attempts=self.apply_calls,
            )
        if self.write_behavior == "business":
            raise PriceError(PriceErrorCode.BUSINESS_ERROR, "商品处于活动锁价期")
        # 成功写入：命中已应用键则回放
        replay = idempotency_key in self.seen_keys
        self.seen_keys.add(idempotency_key)
        if not replay:
            self.side_effects += 1
        return PriceWriteReceipt(
            product_ref=command.product_ref,
            applied_price=command.target_price,
            idempotent_replay=replay,
            attempts=self.apply_calls,
        )

    async def verify_price(self, ref: ProductRef, expected_price: Decimal) -> PriceVerification:
        observed = expected_price if self.verify_consistent else expected_price + Decimal("1")
        return PriceVerification(
            product_ref=ref,
            expected_price=expected_price,
            observed_price=observed,
            consistent=self.verify_consistent,
        )


def make_command(operation_id="op-1", target="100", platform="mock",
                 requester="ops", reason="调价"):
    ref = ProductRef(platform=platform, shop_id="s1", product_id="p1", sku_id="sku1")
    return PriceChangeCommand(
        operation_id=operation_id, product_ref=ref,
        target_price=Decimal(target), requester=requester, reason=reason,
    )


async def run_execute(platform, command, cost=Decimal("50"), store=None, approve=True):
    coord = PriceChangeCoordinator(
        platform, session_id="sess-test", operation_store=store,
        cost_provider=FakeCostProvider(float(cost)),
    )
    op = await coord.execute(command, lambda ev: approve)
    return coord, op


# ---------------------------------------------------------------------------
# 状态集合与非法转移
# ---------------------------------------------------------------------------
def test_all_required_states_exist():
    names = {s.value for s in PriceChangeState}
    assert {
        "CREATED", "PRECHECKED", "WAITING_APPROVAL", "EXECUTING", "VERIFYING",
        "SUCCEEDED", "BLOCKED", "REJECTED", "RETRYING", "UNKNOWN_OUTCOME",
    } <= names


def test_illegal_transition_rejected():
    op = PriceChangeOperation(operation_id="x", command=make_command())
    op.state = PriceChangeState.CREATED
    with pytest.raises(IllegalStateTransition):
        op.transition_to(PriceChangeState.SUCCEEDED)  # CREATED 不能直接到 SUCCEEDED
    with pytest.raises(IllegalStateTransition):
        assert_transition(PriceChangeState.SUCCEEDED, PriceChangeState.EXECUTING, "x")


def test_terminal_states_reject_any_transition():
    for state in (PriceChangeState.SUCCEEDED, PriceChangeState.REJECTED, PriceChangeState.BLOCKED):
        op = PriceChangeOperation(operation_id="x", command=make_command(), state=state)
        for target in PriceChangeState:
            if target is state:
                continue
            with pytest.raises(IllegalStateTransition):
                op.transition_to(target)


# ---------------------------------------------------------------------------
# 全路径：成功闭环
# ---------------------------------------------------------------------------
async def test_happy_path_reaches_succeeded():
    platform = FakePlatform()
    coord, op = await run_execute(platform, make_command())
    assert op.state is PriceChangeState.SUCCEEDED
    assert op.trail == [
        "PRECHECKED", "WAITING_APPROVAL", "EXECUTING", "VERIFYING", "SUCCEEDED",
    ]
    assert platform.apply_calls == 1
    assert platform.side_effects == 1
    assert op.idempotency_key == "price_op-1"


# ---------------------------------------------------------------------------
# BLOCKED：能力缺失 / 成本保护
# ---------------------------------------------------------------------------
async def test_blocked_when_capability_missing():
    # 未登记平台 → 能力检查失败 → BLOCKED
    coord = PriceChangeCoordinator(FakePlatform(platform="ghost"))
    op = await coord.create(make_command(platform="ghost"))
    await coord.precheck(op)
    assert op.state is PriceChangeState.BLOCKED


async def test_blocked_when_below_cost():
    # 目标价低于成本价 → 成本保护拦截 → BLOCKED（且不写入平台）
    platform = FakePlatform()
    coord = PriceChangeCoordinator(
        platform, session_id="s", cost_provider=FakeCostProvider(200.0),
    )
    op = await coord.create(make_command(target="100"))
    await coord.precheck(op)
    assert op.state is PriceChangeState.BLOCKED
    assert "成本保护" in op.rule_summary


# ---------------------------------------------------------------------------
# REJECTED：审批拒绝不产生任何平台副作用
# ---------------------------------------------------------------------------
async def test_approval_denied_no_platform_side_effect():
    platform = FakePlatform()
    coord, op = await run_execute(platform, make_command(), approve=False)
    assert op.state is PriceChangeState.REJECTED
    assert platform.apply_calls == 0
    assert platform.side_effects == 0


# ---------------------------------------------------------------------------
# RETRYING：瞬态错误同键重试
# ---------------------------------------------------------------------------
async def test_transient_retry_reuses_same_idempotency_key():
    platform = FakePlatform(write_behavior="transient_then_ok")
    coord, op = await run_execute(platform, make_command())
    assert op.state is PriceChangeState.SUCCEEDED
    # 两次写入调用，但幂等键相同 → 平台只产生一次副作用
    assert platform.apply_calls == 2
    assert platform.side_effects == 1
    assert set(platform.applied_keys) == {"price_op-1"}


async def test_idempotent_replay_on_duplicate_key():
    platform = FakePlatform()
    cmd = make_command()
    await platform.apply_price(cmd, idempotency_key="price_op-1")
    receipt = await platform.apply_price(cmd, idempotency_key="price_op-1")
    assert receipt.idempotent_replay is True
    assert platform.side_effects == 1


# ---------------------------------------------------------------------------
# UNKNOWN_OUTCOME → VERIFYING → SUCCEEDED：恢复不产生第二次写入
# ---------------------------------------------------------------------------
async def test_unknown_outcome_recovers_without_second_write():
    platform = FakePlatform(write_behavior="unknown")
    coord, op = await run_execute(platform, make_command())
    # 写入超时进入未知结果，回查一致后成功
    assert op.state is PriceChangeState.SUCCEEDED
    assert "UNKNOWN_OUTCOME" in op.trail
    assert op.trail[-3:] == ["UNKNOWN_OUTCOME", "VERIFYING", "SUCCEEDED"]
    # 关键：整个生命周期只发生过一次平台写入，恢复只回查不重写
    assert platform.apply_calls == 1
    assert platform.side_effects == 1


async def test_resume_from_unknown_outcome_does_not_rewrite():
    platform = FakePlatform(write_behavior="unknown")
    coord, op = await run_execute(platform, make_command())
    assert op.state is PriceChangeState.SUCCEEDED
    assert platform.apply_calls == 1
    # 再次对同操作发起恢复（处于 VERIFYING/SUCCEEDED 之外的姿态不重写入）
    op2 = PriceChangeOperation(
        operation_id=op.operation_id, command=op.command,
        state=PriceChangeState.UNKNOWN_OUTCOME, idempotency_key=op.idempotency_key,
    )
    await coord.recover_unknown(op2)
    assert op2.state is PriceChangeState.SUCCEEDED
    assert platform.apply_calls == 1  # 恢复路径未触发第二次写入


# ---------------------------------------------------------------------------
# 回查不一致 → REJECTED
# ---------------------------------------------------------------------------
async def test_verification_mismatch_rejected():
    platform = FakePlatform(verify_consistent=False)
    coord, op = await run_execute(platform, make_command())
    assert op.state is PriceChangeState.REJECTED
    assert op.verification is not None
    assert op.verification.consistent is False


# ---------------------------------------------------------------------------
# 恢复姿态：审批挂起 / 执行中断 / 未知结果
# ---------------------------------------------------------------------------
async def test_resume_from_waiting_approval():
    platform = FakePlatform()
    coord = PriceChangeCoordinator(
        platform, session_id="s", cost_provider=FakeCostProvider(50.0),
    )
    op = await coord.create(make_command())
    await coord.precheck(op)
    coord.begin_approval(op)  # 进入 WAITING_APPROVAL 后持久化中断
    assert op.state is PriceChangeState.WAITING_APPROVAL
    # 恢复：重新审批并继续
    await coord.resume(op, approver=lambda ev: True)
    assert op.state is PriceChangeState.SUCCEEDED


async def test_resume_from_executing_interruption():
    # 模拟写入前中断：状态停在 EXECUTING，恢复后继续写入并成功
    platform = FakePlatform()
    coord = PriceChangeCoordinator(
        platform, session_id="s", cost_provider=FakeCostProvider(50.0),
    )
    op = await coord.create(make_command())
    await coord.precheck(op)
    await coord.request_approval(op, lambda ev: True)
    op.state = PriceChangeState.EXECUTING  # 模拟中断
    await coord.resume(op)
    assert op.state is PriceChangeState.SUCCEEDED
    assert platform.apply_calls == 1


# ---------------------------------------------------------------------------
# 业务错误 → BLOCKED（不可重试）
# ---------------------------------------------------------------------------
async def test_business_error_blocks():
    platform = FakePlatform(write_behavior="business")
    coord, op = await run_execute(platform, make_command())
    assert op.state is PriceChangeState.BLOCKED
    assert platform.apply_calls == 1  # 业务错误不重试


# ---------------------------------------------------------------------------
# 审计落盘（配合 OperationStore）
# ---------------------------------------------------------------------------
async def test_audit_events_recorded(tmp_path):
    store = OperationStore(str(tmp_path))
    platform = FakePlatform()
    await run_execute(platform, make_command(), store=store)
    records = store.load("sess-test")
    types = [r["type"] for r in records]
    assert "operation_created" in types
    assert "approval_decided" in types
    assert "platform_attempt" in types
    assert "verification_observed" in types
    # state_changed 至少覆盖 PRECHECKED → ... → SUCCEEDED
    state_changes = [r for r in records if r["type"] == "state_changed"]
    assert {c["to_state"] for c in state_changes} >= {
        "PRECHECKED", "WAITING_APPROVAL", "EXECUTING", "VERIFYING", "SUCCEEDED",
    }


async def test_unknown_outcome_audit_trail(tmp_path):
    store = OperationStore(str(tmp_path))
    platform = FakePlatform(write_behavior="unknown")
    await run_execute(platform, make_command(), store=store)
    records = store.load("sess-test")
    to_states = [r["to_state"] for r in records if r["type"] == "state_changed"]
    assert "UNKNOWN_OUTCOME" in to_states
    assert to_states[-1] == "SUCCEEDED"
