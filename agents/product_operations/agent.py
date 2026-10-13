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
from runtime.capabilities.skill_activation import LOAD_SKILL_KEY


AGENT_KEY: Final = "product-maintenance"
AGENT_VERSION: Final = 4

PRODUCT_AGENT_DEFINITION: Final = AgentDefinition(
    name="ProductMaintenanceAgent",
    system_prompt_version="product-agent-prompt-v4",
    required_backend_capabilities=frozenset({"streaming", "tool_calling"}),
    required_skill_keys=frozenset({SKILL_KEY}),
    required_tool_keys=frozenset(
        {
            SEARCH_PRODUCTS_DEFINITION.stable_key,
            GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
            COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            STABLE_KEY_UPDATE_PRICE,
            LOAD_SKILL_KEY,
        }
    ),
    required_source_keys=frozenset(),
    # 参考 Agent 的保守运行上限,随 AgentVersion 一起版本化;
    # 这不是 Runtime 的全局默认值(RuntimeDefaults 见 RunLimits 的保守基线)。
    # Runtime 组合根通过 RunLimits.from_definition 消费同一份定义。
    loop_limits={
        # 单次改价任务最多 8 轮模型调用:读取/比较到写入的闭环足够,
        # 又能在模型陷入无界重复时尽早失败。
        "max_model_rounds": 8,
        # 单次 Run 最多 8 次 ToolCall:覆盖查询、比较、加载 Skill 与一次写入。
        "max_tool_calls": 8,
        # 单个 Tool 超时 60 秒:平台读写回执应在秒级返回。
        "tool_timeout_seconds": 60,
        # Run 总时限 1 小时:留出人工确认挂起后恢复的时间。
        "run_deadline_seconds": 3600,
        # 模型调用重试 1 次:改价写路径宁可失败也不无限重试。
        "backend_retries": 1,
        # Reconciliation 预算 3 次:足以覆盖平台回查与补偿,不含无限重试。
        "reconcile_budget": 3,
    },
)


__all__ = ["AGENT_KEY", "AGENT_VERSION", "PRODUCT_AGENT_DEFINITION"]
