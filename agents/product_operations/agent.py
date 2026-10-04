"""ProductMaintenance Agent 组装记录(计划 03 Task 6;架构文档 §3.1)。

Agent 是能力组合的声明:所需 Skill/Tool/Source 稳定 Key 与必需 Backend
能力。``AgentDefinition`` 即 ``AgentVersion.definition_json`` 的组装视图;
Worker 组合根把它注册进 :class:`~runtime.capabilities.registry.AgentRegistry`,
Run 创建时再钉住精确版本。

本模块只 import runtime 与本 Agent 自己的模块;Runtime 不 import 本包
(依赖方向:apps → runtime + agents,agents → runtime)。
"""

from __future__ import annotations

from typing import Final

from agents.product_operations.skill import SKILL_KEY
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import STABLE_KEY_UPDATE_PRICE
from runtime.capabilities.registry import AgentDefinition


AGENT_KEY: Final = "product-maintenance"

PRODUCT_AGENT_DEFINITION: Final = AgentDefinition(
    name="ProductMaintenanceAgent",
    system_prompt_version="product-agent-prompt-v1",
    required_backend_capabilities=frozenset({"streaming", "tool_calling"}),
    required_skill_keys=frozenset({SKILL_KEY}),
    required_tool_keys=frozenset(
        {
            SEARCH_PRODUCTS_DEFINITION.stable_key,
            GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
            COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            STABLE_KEY_UPDATE_PRICE,
        }
    ),
    required_source_keys=frozenset(),
)


__all__ = ["AGENT_KEY", "PRODUCT_AGENT_DEFINITION"]
