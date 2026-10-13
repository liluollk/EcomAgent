"""商品 Agent 组装不变式测试(计划 03 Task 6;架构文档 §3、§4)。

覆盖:
- 商品业务 Tool 与 Runtime 内置 ``load_skill``、Agent/Skill 版本
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
    AGENT_VERSION,
    PRODUCT_AGENT_DEFINITION,
)
from agents.product_operations.prompt import PRODUCT_AGENT_PROMPT
from agents.product_operations.skill import (
    PRODUCT_MAINTENANCE_SKILL,
    PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS,
    SKILL_KEY,
    SKILL_VERSION,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import (
    STABLE_KEY_UPDATE_PRICE,
    UPDATE_PRICE_DEFINITION,
)
from agents.product_operations.tools.update_price import (
    TOOL_VERSION as UPDATE_PRICE_TOOL_VERSION,
)
from runtime.backend.protocol import BackendCompleted, TextDelta, ToolCallCompleted
from runtime.capabilities.skill_activation import LOAD_SKILL_KEY
from runtime.interfaces.state import RunStatus
from runtime.interfaces.tools import ToolEffect
from runtime.orchestration.limits import RunLimits


PRODUCT_TOOL_KEYS = (
    LOAD_SKILL_KEY,
    SEARCH_PRODUCTS_DEFINITION.stable_key,
    GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
    COMPARE_LISTING_PRICES_DEFINITION.stable_key,
    STABLE_KEY_UPDATE_PRICE,
)


async def test_product_and_runtime_tools_are_registered(
    product_application,
) -> None:
    """组装后:业务/Runtime Tool 均注册绑定;Agent/Skill 需求一致。"""
    assembly = product_application.assembly
    assembly.validate()

    registered = {record.stable_key: record for record in assembly.tools.records()}
    assert set(registered) == set(PRODUCT_TOOL_KEYS)
    for key in PRODUCT_TOOL_KEYS:
        record = registered[key]
        expected_version = 3 if key == STABLE_KEY_UPDATE_PRICE else 2
        if key == LOAD_SKILL_KEY:
            expected_version = 1
        assert record.version == expected_version
        assert assembly.tools.binding_for_key(key), f"tool {key} 缺少 ToolBinding"

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
    assert AGENT_VERSION == 4
    assert SKILL_VERSION == 4
    assert PRODUCT_AGENT_DEFINITION.system_prompt_version == "product-agent-prompt-v4"
    assert UPDATE_PRICE_TOOL_VERSION == 3


def test_agent_definition_carries_reference_loop_limits() -> None:
    """版本化定义携带参考 Agent 的保守运行上限(非 Runtime 全局默认)。"""
    assert PRODUCT_AGENT_DEFINITION.loop_limits == {
        "max_model_rounds": 8,
        "max_tool_calls": 8,
        "tool_timeout_seconds": 60,
        "run_deadline_seconds": 3600,
        "backend_retries": 1,
        "reconcile_budget": 3,
        "write_retry_budget": 1,
    }


def test_run_limits_follow_agent_definition_not_private_constant(
    product_application,
) -> None:
    """运行限制来自 ``AgentDefinition.loop_limits``,组合根不再持有私有常量。

    同一份定义必须同时驱动 Runtime 构造与 Reconciliation 预算,避免出现
    定义已改、运行限制仍钉在模块级 ``_LIMITS`` 的漂移。
    """
    from composition import product_operations as composition_module

    assert not hasattr(composition_module, "_LIMITS")
    assert product_application.run_limits == RunLimits.from_definition(
        PRODUCT_AGENT_DEFINITION.loop_limits
    )


async def test_update_price_model_visible_fields_are_exactly_store_sku_price(
    product_application,
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
    product_application,
) -> None:
    """Prompt/Skill 不复制代码 Policy 数字(最低允许价、变动阈值、种子现价)。"""
    config = product_application.price_config
    forbidden_numbers = (
        str(config.min_allowed_price),
        str(config.max_price_change),
        str(Decimal("128.00")),
        "50",
        "100",
        "128",
    )
    for text in (product_application.agent_prompt, product_application.skill_instructions):
        for number in forbidden_numbers:
            assert number not in text, f"Prompt/Skill 泄漏 Policy 数字 {number}"


def test_agent_prompt_requires_loading_matching_skill() -> None:
    assert "load_skill" in PRODUCT_AGENT_PROMPT
    assert SKILL_KEY in PRODUCT_AGENT_PROMPT


def test_confirmation_is_owned_by_runtime_execution_mode() -> None:
    for text in (PRODUCT_AGENT_PROMPT, PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS):
        assert "ExecutionMode" in text
        assert "AUTO_EXECUTE" in text
        assert "所有改价都必须确认" not in text
        assert "绝不未经确认执行写操作" not in text


def test_update_price_description_defers_confirmation_to_execution_mode() -> None:
    description = UPDATE_PRICE_DEFINITION.description
    assert "ExecutionMode" in description
    assert "用户确认" not in description


def test_write_task_instructions_continue_after_reading() -> None:
    """用户要求写入时，读取和比较只是前置步骤，不是任务终点。"""
    for text in (PRODUCT_AGENT_PROMPT, PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS):
        assert "读取或比较不是任务终点" in text
        assert "update_price" in text


def test_write_task_instructions_preserve_user_business_identifiers() -> None:
    """后续写调用复用用户业务标识，不把 ToolResult 内部 ID 当作授权来源。"""
    for text in (PRODUCT_AGENT_PROMPT, PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS):
        assert "用户当前消息中的店铺名称或别名" in text
        assert "ToolResult" in text
        assert "内部 ID" in text


def test_tool_input_schema_describes_authoritative_argument_sources() -> None:
    update_properties = UPDATE_PRICE_DEFINITION.input_json_schema["properties"]
    assert "用户当前消息" in update_properties["store_id"]["description"]
    assert "内部 ID" in update_properties["store_id"]["description"]
    assert "用户当前消息" in update_properties["sku"]["description"]
    assert "计算" in update_properties["target_price"]["description"]

    snapshot_properties = GET_PRODUCT_SNAPSHOT_DEFINITION.input_json_schema["properties"]
    assert "用户" in snapshot_properties["store_id"]["description"]
    compare_properties = COMPARE_LISTING_PRICES_DEFINITION.input_json_schema["properties"]
    assert "候选店铺" in compare_properties["store_ids"]["description"]


async def test_product_skill_is_loaded_only_after_model_requests_it(
    product_application,
) -> None:
    backend = product_application.scripted_backend
    backend.set_rounds(
        [
            [
                ToolCallCompleted(
                    call_id="load-skill-1",
                    name=LOAD_SKILL_KEY,
                    arguments={"skill_key": SKILL_KEY},
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [
                TextDelta(text_delta="已根据商品维护技能完成分析。"),
                BackendCompleted(finish_reason="stop"),
            ],
        ]
    )

    result = await product_application.start("查询商品并判断是否需要调整价格")

    assert result.status is RunStatus.COMPLETED
    assert len(backend.requests) == 2
    first_context = "\n".join(message.content for message in backend.requests[0].messages)
    second_context = "\n".join(message.content for message in backend.requests[1].messages)
    assert SKILL_KEY in first_context
    assert PRODUCT_MAINTENANCE_SKILL.description in first_context
    assert PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS not in first_context
    assert PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS in second_context
    assert LOAD_SKILL_KEY in {
        definition.stable_key for definition in backend.requests[0].tool_definitions
    }


async def test_explicit_store_alias_is_normalized_before_read_tool_execution(
    product_application,
) -> None:
    backend = product_application.scripted_backend
    backend.set_rounds(
        [
            [
                ToolCallCompleted(
                    call_id="load-skill-alias",
                    name=LOAD_SKILL_KEY,
                    arguments={"skill_key": SKILL_KEY},
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [
                ToolCallCompleted(
                    call_id="snapshot-alias",
                    name=GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
                    arguments={"store_id": "淘宝旗舰店", "sku": "SKU-001"},
                ),
                BackendCompleted(finish_reason="tool_calls"),
            ],
            [
                TextDelta(text_delta="淘宝旗舰店 SKU-001 当前售价为 128.00 元。"),
                BackendCompleted(finish_reason="stop"),
            ],
        ]
    )

    result = await product_application.start("查看淘宝旗舰店 SKU-001 当前售价。")

    assert result.status is RunStatus.COMPLETED
    assert len(backend.requests) == 3
    read_result = next(
        message
        for message in backend.requests[2].messages
        if message.role == "tool"
        and message.name == GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key
    )
    assert str(product_application.seed_ids.store_id) in read_result.content
    assert "SKU-001" in read_result.content
