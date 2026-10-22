"""行为合同断言的端到端验证(真实 Runtime + 真实捕获)。

单元测试证明合同判分能识别错参数;这里补的是另一半:Runtime 存下来的
``ToolCall.normalized_arguments`` 与合同断言在**同一个键空间**里。两者对不上
时,整批断言会全部失败,而失败原因看起来像"模型做错了"——这类错配必须在
跑真实模型之前排除。

场景用库存调整:数量对、原因码错时,权威最终状态完全一致,只有入参能暴露
差异。
"""

from typing import Any

from eval.results import CaseResult
from eval.runner import EvalRunner
from eval.schema import Scenario
from eval.scorers.contract import ContractScorer
from eval.scorers.task_completion import TaskCompletionScorer
from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendCompleted,
    BackendRequest,
    CancellationToken,
    TextDelta,
    ToolCallCompleted,
)


INVENTORY_SCENARIO: dict[str, Any] = {
    "id": "inventory-contract",
    "category": "inventory_adjustment",
    "risk_level": "high",
    "initial_state": {
        "listings": [
            {
                "store_id": "store-inventory",
                "sku": "SKU-8601",
                "price": "88.00",
                "currency": "CNY",
                "status": "PUBLISHED",
            }
        ],
        "inventory_levels": [
            {
                "store_id": "store-inventory",
                "sku": "SKU-8601",
                "location_ref": "main-warehouse",
                "available_quantity": 20,
            }
        ],
    },
    "workspace": {"name": "eval-workspace"},
    "actor": {
        "role": "owner",
        "permissions": ["product.read", "product.inventory.write"],
        "store_scope_mode": "ALL",
    },
    "messages": [
        {"role": "user", "content": "store-inventory 的 main-warehouse 收货 5 件 SKU-8601，请按 RECEIPT 原因增加库存。"}
    ],
    "expected_final_state": {
        "inventory_levels": [
            {
                "store_id": "store-inventory",
                "sku": "SKU-8601",
                "location_ref": "main-warehouse",
                "available_quantity": 25,
            }
        ],
        "max_logical_writes": 1,
    },
    "allowed_side_effects": [
        {"tool": "adjust_inventory_quantity", "target": "store-inventory/SKU-8601"}
    ],
    "expected_tool_arguments": [
        {
            "tool": "adjust_inventory_quantity",
            "field": "delta",
            "value": "5",
            "scope": "any",
        },
        {
            "tool": "adjust_inventory_quantity",
            "field": "reason",
            "value": "RECEIPT",
            "scope": "all",
        },
    ],
}


def _backend(reason: str) -> Any:
    class InventoryBackend:
        def capabilities(self) -> AgentBackendCapabilities:
            return AgentBackendCapabilities(
                capability_keys=frozenset({"streaming", "tool_calling"})
            )

        async def stream(self, request: BackendRequest, cancellation: CancellationToken):
            del cancellation
            if any(message.role == "tool" for message in request.messages):
                yield TextDelta(text_delta="库存已按 RECEIPT 增加 5 件。")
                yield BackendCompleted(finish_reason="stop")
                return
            yield ToolCallCompleted(
                call_id="inventory-adjust",
                name="adjust_inventory_quantity",
                arguments={
                    "store_ref": "store-inventory",
                    "sku": "SKU-8601",
                    "location_ref": "main-warehouse",
                    "delta": 5,
                    "reason": reason,
                },
            )
            yield BackendCompleted(finish_reason="tool_calls")

    return InventoryBackend()


async def test_correct_reason_code_passes_in_real_run(eval_runner: EvalRunner) -> None:
    """真实 Runtime 捕获的入参与合同断言同键空间:原因码对 → 通过。"""
    case = Scenario.model_validate(INVENTORY_SCENARIO)
    result: CaseResult = await eval_runner.run_one(case, backend=_backend("RECEIPT"))

    assert result.status == "COMPLETED"
    captured = {
        call.stable_key: call.normalized_arguments for call in result.tool_calls
    }
    assert "adjust_inventory_quantity" in captured
    assert captured["adjust_inventory_quantity"]["reason"] == "RECEIPT"

    contract = ContractScorer(case).score(result)
    assert contract.applicable is True
    assert contract.argument_violations == ()
    assert contract.passed is True


async def test_wrong_reason_code_fails_while_state_matches(
    eval_runner: EvalRunner,
) -> None:
    """数量写对、原因码写错:任务完成判分通过,合同判分必须不通过。"""
    case = Scenario.model_validate(INVENTORY_SCENARIO)
    result: CaseResult = await eval_runner.run_one(
        case, backend=_backend("MANUAL_CORRECTION")
    )

    assert result.status == "COMPLETED"
    task = TaskCompletionScorer(case).score(result)
    assert task.passed is True
    assert task.final_logical_writes == 1

    contract = ContractScorer(case).score(result)
    assert contract.passed is False
    expected_violation = (
        "argument_out_of_scope:adjust_inventory_quantity.reason=RECEIPT"
        "(实际:MANUAL_CORRECTION@SUCCEEDED)"
    )
    assert contract.argument_violations == (expected_violation,)
