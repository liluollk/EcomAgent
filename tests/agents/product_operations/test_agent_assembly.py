"""商品 Agent 组装不变式测试(计划 03 Task 6;架构文档 §3、§4)。

覆盖:
- 四个商品 Tool 定义(三个 READ + update_price WRITE)与 Agent/Skill 版本
  均已注册,Agent 所需 Tool/Skill Key 与必需 Backend 能力声明一致;
- ``update_price`` 模型可见字段恰为 ``store_id/sku/target_price``(Input
  JSON Schema 的 properties 与 required),currency、Listing Version、
  Policy Version、operation_id 与幂等键均不在模型可见输入中;
- Agent Prompt 与 Skill 指令只描述任务方法,不包含代码 Policy 的具体数字
  (最低允许价、单次变动阈值、种子现价)。

Runtime 不 import agents 的架构断言已由 ``tests/architecture``
(test_dependency_boundaries)覆盖,apps 亦在其 FORBIDDEN 列表内,不重复。
"""

from __future__ import annotations

from decimal import Decimal

from agents.product_operations.agent import (
    AGENT_KEY,
    PRODUCT_AGENT_DEFINITION,
)
from agents.product_operations.skill import SKILL_KEY
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import (
    STABLE_KEY_UPDATE_PRICE,
    UPDATE_PRICE_DEFINITION,
)
from runtime.interfaces.tools import ToolEffect


PRODUCT_TOOL_KEYS = (
    SEARCH_PRODUCTS_DEFINITION.stable_key,
    GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
    COMPARE_LISTING_PRICES_DEFINITION.stable_key,
    STABLE_KEY_UPDATE_PRICE,
)


async def test_four_product_tools_and_agent_are_registered(
    product_runtime,
) -> None:
    """组装后:四个 Tool 版本注册且各有绑定;Agent/Skill 版本注册且需求一致。"""
    assembly = product_runtime.assembly
    assembly.validate()

    registered = {record.stable_key: record for record in assembly.tools.records()}
    assert set(registered) == set(PRODUCT_TOOL_KEYS)
    for key in PRODUCT_TOOL_KEYS:
        record = registered[key]
        assert record.version == 1
        assert assembly.tools.bindings_for(key), f"tool {key} 缺少 ToolBinding"

    update_price = registered[STABLE_KEY_UPDATE_PRICE]
    assert update_price.effect == ToolEffect.WRITE
    assert update_price.required_permissions == frozenset({"product.price.write"})

    agent = assembly.agents.find_by_key(AGENT_KEY)
    assert agent is not None
    assert agent.definition.required_tool_keys == frozenset(PRODUCT_TOOL_KEYS)
    assert agent.definition.required_skill_keys == frozenset({SKILL_KEY})
    assert agent.definition.required_backend_capabilities == frozenset(
        PRODUCT_AGENT_DEFINITION.required_backend_capabilities
    )
    assert assembly.skills.find_by_key(SKILL_KEY) is not None


async def test_update_price_model_visible_fields_are_exactly_store_sku_price(
    product_runtime,
) -> None:
    """update_price 的 Input Schema 恰暴露 store_id/sku/target_price 三字段。"""
    input_schema = UPDATE_PRICE_DEFINITION.input_json_schema
    properties = input_schema.get("properties")
    assert isinstance(properties, dict)
    assert set(properties) == {"store_id", "sku", "target_price"}
    assert set(input_schema.get("required", [])) == {"store_id", "sku", "target_price"}

    # 服务端补全字段绝不进入模型可见输入;输出回执只携带观测结果。
    output_properties = UPDATE_PRICE_DEFINITION.output_json_schema.get("properties")
    assert isinstance(output_properties, dict)
    assert not ({"currency", "operation_id", "expected_version"} & set(properties))
    assert UPDATE_PRICE_DEFINITION.effect == ToolEffect.WRITE


async def test_agent_prompt_and_skill_do_not_leak_policy_numbers(
    product_runtime,
) -> None:
    """Prompt/Skill 不复制代码 Policy 数字(最低允许价、变动阈值、种子现价)。"""
    config = product_runtime.price_config
    forbidden_numbers = (
        str(config.min_allowed_price),
        str(config.max_price_change),
        str(Decimal("128.00")),
        "50",
        "100",
        "128",
    )
    for text in (product_runtime.agent_prompt, product_runtime.skill_instructions):
        for number in forbidden_numbers:
            assert number not in text, f"Prompt/Skill 泄漏 Policy 数字 {number}"
