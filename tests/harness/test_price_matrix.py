"""调价闭环评测矩阵单测 — 场景表本身的形状与覆盖度。

harness 是「行为契约的单一事实源」：场景表错了，评测就会在错误的契约上
绿着。本文件不加断言逻辑，只校验场景表**声明得对**：

  1. 21 条场景、四类分布、两个平台都覆盖到；
  2. 每条场景都声明了 platform，且执行契约（step.expect）与场景类型自洽
     （守门类不产生操作/副作用、恢复类必须能重建终态）；
  3. 每类契约至少被一条场景覆盖（指标集合不留空洞）；
  4. 故障脚本名都在 mock 侧存在，且带语义的脚本其目标操作与场景意图一致。

另外补一条**第二道闸**的直连测试：默认装配里成本保护的早闸在引擎侧
（workspace_rules_rule），协调器内部还有一道同语义的检查——早闸未配置时
第二道闸必须独立拦住，这是「双闸」中容易被忽略的一半。
"""

from __future__ import annotations

import pytest

from harness import cases as cases_mod
from harness.cases import SCENARIOS, VALID_KINDS, VALID_METRICS, real_model_scenarios
from harness.faults import FAULT_SLEEP_SECONDS, _fast_policy  # noqa: F401 (启用条件自检)

EXPECTED_TOTAL = 21
EXPECTED_BY_KIND = {"gold": 3, "guarded": 7, "resilience": 6, "recovery": 5}


def _expects(scenario):
    return [step["expect"] for step in scenario["steps"] if step.get("expect")]


# ---------------------------------------------------------------------------
# 场景表形状
# ---------------------------------------------------------------------------


def test_scenario_count_and_kind_distribution():
    assert len(SCENARIOS) == EXPECTED_TOTAL
    counts: dict[str, int] = {}
    for s in SCENARIOS:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    assert counts == EXPECTED_BY_KIND
    assert set(counts) == set(VALID_KINDS)
    # 旧分类不残留
    assert "ambiguous" not in VALID_KINDS


def test_every_scenario_declares_platform_and_two_platforms_are_covered():
    platforms = {s["platform"] for s in SCENARIOS}
    assert platforms == {"taobao", "douyin", "both"}
    # 两条平台线都要有独占场景，避免「只跑了一个平台」的假覆盖
    assert sum(1 for s in SCENARIOS if s["platform"] == "taobao") >= 8
    # 抖店至少要有一条独占的写路径场景与一条独占的审批路径场景，
    # 否则「跨平台一致」只剩淘宝在自己验自己
    assert sum(1 for s in SCENARIOS if s["platform"] == "douyin") >= 2


def test_every_scenario_has_execution_contract():
    """每条场景的每个业务步都必须声明执行契约——否则只剩文本断言。"""
    for s in SCENARIOS:
        expects = _expects(s)
        assert len(expects) == len(s["steps"]), f"{s['name']} 有步骤缺 expect"
        for step in s["steps"]:
            # 决策契约与执行契约至少各有一条可验证断言
            assert step.get("tool") or step.get("expect_tool") is False, s["name"]
            if step.get("expect_tool") is not False:
                assert step.get("result_contains") or step["expect"].get("state"), s["name"]


def test_guarded_scenarios_write_only_when_approval_lets_them():
    """守门类场景的底线：被拦截 / 被拒绝的步必须零副作用；放行的审批恰好写一次。"""
    for s in SCENARIOS:
        if s["kind"] != "guarded":
            continue
        for step in s["steps"]:
            expect = step["expect"]
            blocked = (
                expect.get("operation_created") is False
                or expect.get("state") in {"BLOCKED", "REJECTED"}
            )
            if blocked:
                assert expect.get("writes_delta") == 0, f"{s['name']} 被拦截却产生了副作用"
                assert expect.get("platform_attempts") in (0, None) or (
                    expect.get("attempt_outcomes") == ["error"]
                ), f"{s['name']} 被拦截却出现了成功调用"
            else:
                # 唯一被允许的写：审批通过后执行成功，且只写一次
                assert expect.get("state") == "SUCCEEDED", s["name"]
                assert expect.get("writes_delta") == 1, s["name"]
                assert step.get("permission") == "approve", s["name"]


def test_recovery_scenarios_end_in_a_reconstructable_terminal_state():
    """恢复类场景必须收口到可对账的终态（SUCCEEDED / BLOCKED / REJECTED）。"""
    terminal = {"SUCCEEDED", "BLOCKED", "REJECTED"}
    for s in SCENARIOS:
        if s["kind"] != "recovery":
            continue
        for step in s["steps"]:
            assert step["expect"].get("state") in terminal, s["name"]
            # 有副作用的恢复场景必须断言「只写一次」
            if step["expect"].get("writes_delta", 0) > 0:
                assert step["expect"].get("platform_attempts") is not None, s["name"]


def test_every_metric_used_is_declared_and_each_kind_has_metrics():
    used = {m for s in SCENARIOS for m in s["metrics"]}
    assert used <= VALID_METRICS
    for kind in VALID_KINDS:
        kind_metrics = {m for s in SCENARIOS if s["kind"] == kind for m in s["metrics"]}
        assert kind_metrics, f"{kind} 类没有声明任何领域指标"


def test_price_workflow_contracts_are_all_covered():
    """调价闭环的核心契约逐项被至少一条场景覆盖（覆盖率空洞的自检）。"""
    used = {m for s in SCENARIOS for m in s["metrics"]}
    required = {
        # 决策 + 正常闭环
        "tool_routing", "platform_matrix", "price_verification",
        # 守门
        "constraint_violation_blocked", "activity_lock_blocked", "capability_unsupported",
        "permission_denied", "mode_gate_block", "hitl_approve", "hitl_reject",
        # 执行可靠性
        "retry_contract", "unknown_outcome_guard", "fatal_no_retry",
        "schema_validation", "verify_retry_contract",
        # 恢复与对账
        "operation_state_machine", "unknown_outcome_recovery",
        "verify_mismatch_detected", "idempotent_key_reuse", "audit_integrity",
    }
    assert required <= used


def test_real_mode_subset_covers_all_execution_contract_kinds():
    subset = real_model_scenarios()
    kinds = {s["kind"] for s in subset}
    # 决策契约场景（gold）不进真实模式；执行契约三类必须都在
    assert kinds == {"guarded", "resilience", "recovery"}
    for s in subset:
        assert s["platform"], s["name"]


def test_fault_scripts_exist_and_target_expected_operation():
    """场景引用的故障脚本必须存在；带语义的脚本必须钉在预期的调价操作上。"""
    from mock_commerce import fault_injection

    known = set(fault_injection._FAULT_SCRIPTS)  # noqa: SLF001 - 断言脚本表本身
    for s in SCENARIOS:
        fault = s.get("fault")
        if fault:
            assert fault in known, f"{s['name']} 引用了不存在的故障脚本 {fault}"

    # 写故障必须落在 apply_price、回查故障必须落在 verify，否则场景会假阴性
    assert fault_injection.default_operations("write_timeout_before_commit") == {"apply_price"}
    assert fault_injection.default_operations("write_timeout_after_commit") == {"apply_price"}
    assert fault_injection.default_operations("capability_refused") == {"apply_price"}
    assert fault_injection.default_operations("verify_timeout") == {"verify"}
    assert fault_injection.default_operations("verify_timeout_permanent") == {"verify"}
    assert fault_injection.default_operations("verify_mismatch") == {"verify"}
    # 通用脚本不带定向：需要定向时由场景显式声明
    assert fault_injection.default_operations("timeout_once_then_success") is None


def test_fault_timeout_outlasts_fast_policy_write_timeout():
    """写超时必须真的等得到：故障睡眠 > 快策略的写超时。"""
    policy = _fast_policy()
    assert FAULT_SLEEP_SECONDS > policy.timeout.timeout_for("update_price")


# ---------------------------------------------------------------------------
# 第二道闸：早闸未配置时，协调器内部的成本保护必须独立拦住
# ---------------------------------------------------------------------------


def test_coordinator_cost_gate_blocks_without_engine_rule():
    """workspace_rules_rule 未配置时，协调器自己的规则校验仍要拦下低于成本的调价。

    双闸的两半各守一处：引擎早闸（PreToolUse）拦在工具执行之前；协调器内部
    复用的同一套成本保护语义拦在平台写入之前。场景表的 cost 场景走的是早闸，
    本用例补上第二道闸——它才是「绕过引擎直接调工具」时的最后一道防线。
    """
    import asyncio
    from decimal import Decimal

    from execution.price_change import PriceChangeCoordinator, PriceChangeState
    from integrations.commerce.price_models import (
        PriceChangeCommand,
        PriceConstraints,
        PriceVerification,
        PriceWriteReceipt,
        ProductRef,
        ProductSnapshot,
    )

    class _FakePlatform:
        """假平台：只实现调价执行面，用于隔离协调器自身的逻辑。"""

        platform = "taobao"

        def __init__(self):
            self.writes = 0

        async def query_snapshot(self, ref):
            return ProductSnapshot(
                product_ref=ref, current_price=Decimal("89.00"), stock=100,
                status="on_sale", activity_name="", activity_locked=False,
            )

        async def apply_price(self, command, *, idempotency_key=None):
            self.writes += 1
            return PriceWriteReceipt(product_ref=command.product_ref,
                                     applied_price=command.target_price)

        async def verify_price(self, ref, expected_price):
            return PriceVerification(product_ref=ref, expected_price=expected_price,
                                     observed_price=expected_price, consistent=True)

    platform = _FakePlatform()
    coordinator = PriceChangeCoordinator(
        platform=platform,
        cost_provider=_StubCost(59.0),
        operation_store=None,
    )
    command = PriceChangeCommand(
        operation_id="op-matrix-cost-gate",
        product_ref=ProductRef(platform="taobao", shop_id="s", product_id="ITEM-1001",
                               sku_id="SKU-002"),
        target_price=Decimal("10"),
        requester="role:manager",
        reason="",
    )

    op = asyncio.run(coordinator.execute(command, approver=lambda _e: True))

    assert op.state == PriceChangeState.BLOCKED
    assert "成本保护" in (op.error or op.rule_summary)
    assert platform.writes == 0, "低于成本价绝不允许打平台"


class _StubCost:
    """固定成本价的 CostProvider 桩（成本只来自内部提供方，不由命令传入）。"""

    def __init__(self, cost: float) -> None:
        self._cost = cost

    def get_cost_price(self, channel, sku):
        return self._cost


def test_scenario_table_has_no_extension_tools():
    """默认评测集不许引用扩展工具——它们不在默认 Agent 的工具表里。"""
    import sources.builtin_tools as bt

    default_names = set(bt.tool_names())
    extension_names = set(bt.EXTENSION_TOOL_NAMES)
    assert default_names == {"query_product_snapshot", "update_price", "save_skill"}
    assert extension_names and not (extension_names & default_names)

    for s in SCENARIOS:
        for step in s["steps"]:
            tool = step.get("tool")
            if tool and tool != "load_skill":
                assert tool in default_names, f"{s['name']} 引用了非默认工具 {tool}"


def test_validate_scenarios_rejects_unknown_platform():
    with pytest.raises(ValueError):
        cases_mod.validate_scenarios([{"name": "x", "steps": [], "metrics": []}])
