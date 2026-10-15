"""ProductOperations Agent 与第一版 ProductMaintenance 兼容定义。

当前版本声明 Catalog、Pricing、Inventory、Listing 四个能力模块的 Tool；旧定义
只供历史 Run 按精确 AgentVersion/ToolVersion 恢复。Task 11 仍保持 ADR-001 实施前
的 Skill 模型：Agent 必需 ``product_maintenance``，并显式暴露 ``load_skill``。
"""

from __future__ import annotations

from typing import Final

from agents.product_operations.agent_tool_keys import P1_BUSINESS_TOOL_KEYS
from agents.product_operations.skill import SKILL_KEY
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION as LEGACY_COMPARE_LISTING_PRICES_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    GET_PRODUCT_SNAPSHOT_DEFINITION as LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    SEARCH_PRODUCTS_DEFINITION as LEGACY_SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import STABLE_KEY_UPDATE_PRICE
from runtime.capabilities.registry import AgentDefinition
from runtime.capabilities.skill_activation import LOAD_SKILL_KEY


AGENT_KEY: Final = "product-operations"
AGENT_VERSION: Final = 1

LEGACY_AGENT_KEY: Final = "product-maintenance"
LEGACY_AGENT_VERSION: Final = 4

P1_TOOL_KEYS: Final = P1_BUSINESS_TOOL_KEYS

P1_LOOP_LIMITS: Final = {
    "max_model_rounds": 12,
    "max_tool_calls": 12,
    "tool_timeout_seconds": 60,
    "run_deadline_seconds": 3600,
    "backend_retries": 1,
    "reconcile_budget": 3,
    "write_retry_budget": 1,
}

PRODUCT_AGENT_DEFINITION: Final = AgentDefinition(
    name="ProductOperationsAgent",
    system_prompt_version="product-operations-prompt-v1",
    required_backend_capabilities=frozenset({"streaming", "tool_calling"}),
    required_skill_keys=frozenset({SKILL_KEY}),
    required_tool_keys=P1_TOOL_KEYS | {LOAD_SKILL_KEY},
    required_source_keys=frozenset(),
    loop_limits=P1_LOOP_LIMITS,
)

LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION: Final = AgentDefinition(
    name="ProductMaintenanceAgent",
    system_prompt_version="product-agent-prompt-v4",
    required_backend_capabilities=frozenset({"streaming", "tool_calling"}),
    required_skill_keys=frozenset({SKILL_KEY}),
    required_tool_keys=frozenset(
        {
            LEGACY_SEARCH_PRODUCTS_DEFINITION.stable_key,
            LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
            LEGACY_COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            STABLE_KEY_UPDATE_PRICE,
            LOAD_SKILL_KEY,
        }
    ),
    required_source_keys=frozenset(),
    loop_limits={
        "max_model_rounds": 8,
        "max_tool_calls": 8,
        "tool_timeout_seconds": 60,
        "run_deadline_seconds": 3600,
        "backend_retries": 1,
        "reconcile_budget": 3,
        "write_retry_budget": 1,
    },
)


__all__ = [
    "AGENT_KEY",
    "AGENT_VERSION",
    "LEGACY_AGENT_KEY",
    "LEGACY_AGENT_VERSION",
    "LEGACY_PRODUCT_MAINTENANCE_AGENT_DEFINITION",
    "P1_LOOP_LIMITS",
    "P1_TOOL_KEYS",
    "PRODUCT_AGENT_DEFINITION",
]
