"""Eval Runner 测试夹具(计划 05 Task 2)。

- ``eval_runner``:最小组装的 EvalRunner——backend 由夹具注入(每 case 一个
  ``E2EScriptedBackend``,复用 apps/worker/e2e.py 的确定性脚本模型;tests
  层不受"eval 不 import apps"约束,eval 包本身保持不依赖 apps);
- ``valid_case`` / ``second_case``:合法定价场景(权威期望必填,与
  tests/eval/test_schema.py 的种子风格一致);
- ``provider_401`` / ``connection_error_backend``:确定性基础设施失败
  Backend(在流开始前抛出,不产生任何模型轮次)。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.worker.e2e import E2EScriptedBackend
from eval.config import EvalConfig, fixed_eval_config
from eval.runner import EvalProviderError, EvalRunner
from eval.schema import Scenario
from runtime.backend.protocol import (
    AgentBackendCapabilities,
    BackendEvent,
    BackendRequest,
    CancellationToken,
)


def _price_scenario_dict(scenario_id: str, *, target_price: str) -> dict[str, Any]:
    """最小合法定价场景:店铺/商品符号 → 权威期望(价格字符串保十进制)。"""
    return {
        "id": scenario_id,
        "category": "product_operations",
        "risk_level": "medium",
        "initial_state": {
            "listings": [
                {
                    "store_id": "store-1",
                    "sku": "SKU-001",
                    "price": "128.00",
                    "currency": "CNY",
                    "status": "ACTIVE",
                }
            ]
        },
        "workspace": {"name": "eval-workspace"},
        "actor": {
            "role": "owner",
            "permissions": ["product.read", "product.price.write"],
            "store_scope_mode": "ALL",
        },
        "messages": [
            {"role": "user", "content": f"把 SKU-001 改成 {target_price} 元"},
        ],
        "interactions": [
            {"kind": "input", "values": {"store_id": "store-1"}},
            {"kind": "confirmation", "decision": "accept"},
        ],
        "expected_final_state": {
            "listings": [{"store_id": "store-1", "sku": "SKU-001", "price": target_price}],
            "max_logical_writes": 1,
        },
        "allowed_side_effects": [
            {"tool": "update_price", "target": "store-1/SKU-001"},
        ],
    }


@pytest.fixture()
def eval_config() -> EvalConfig:
    """全仓库唯一固定运行配置(不含任何 API Key)。"""
    return fixed_eval_config()


@pytest.fixture()
def eval_runner(engine: AsyncEngine, eval_config: EvalConfig) -> EvalRunner:
    """最小组装 EvalRunner:注入确定性脚本 Backend,单次重复以控制测试时长。"""
    return EvalRunner(
        engine,
        config=eval_config,
        backend_factory=lambda scenario: E2EScriptedBackend(),
        repetitions=1,
    )


@pytest.fixture()
def valid_case() -> Scenario:
    """完整合法场景:把 SKU-001 从 128.00 改成 99.00。"""
    return Scenario.model_validate(_price_scenario_dict("price-001", target_price="99.00"))


@pytest.fixture()
def second_case() -> Scenario:
    """第二个独立场景:同一初始状态、不同目标价,用于隔离与顺序测试。"""
    return Scenario.model_validate(_price_scenario_dict("price-002", target_price="120.00"))


@pytest.fixture()
def two_cases(valid_case: Scenario, second_case: Scenario) -> list[Scenario]:
    return [valid_case, second_case]


class _RaisingBackend:
    """在流开始前抛出确定性异常的 Backend(不产生任何模型轮次)。

    能力声明与商品 Agent 要求对齐(streaming + tool_calling),使组装校验
    通过、异常发生在模型流边界——这才是真实的 Provider 失败位置。
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def capabilities(self) -> AgentBackendCapabilities:
        return AgentBackendCapabilities(
            capability_keys=frozenset({"streaming", "tool_calling"})
        )

    async def stream(
        self, request: BackendRequest, cancellation: CancellationToken
    ) -> AsyncIterator[BackendEvent]:
        del request, cancellation
        raise self._error
        yield  # pragma: no cover - 使本函数成为 async generator(异常在迭代时抛出)


@pytest.fixture()
def provider_401() -> _RaisingBackend:
    """Provider 401:鉴权失败属基础设施错误,与被测 Agent 行为无关。"""
    return _RaisingBackend(EvalProviderError("provider returned 401", status=401))


@pytest.fixture()
def connection_error_backend() -> _RaisingBackend:
    """连接失败:OSError 系异常同样归基础设施错误。"""
    return _RaisingBackend(OSError("connection refused by provider"))
