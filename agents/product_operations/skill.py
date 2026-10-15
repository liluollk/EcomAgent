"""``product_maintenance`` Skill 的当前版本与第一版兼容版本。

当前 v5 扩展到 Catalog、Pricing、Inventory、Listing 四个能力模块；v4 仅供历史
Run 按精确 SkillVersion/ToolVersion 恢复。这里发布兼容版本，不实施 ADR-001 中
尚未落地的目标加载模型。
"""

from typing import Final

from agents.product_operations.agent_tool_keys import P1_BUSINESS_TOOL_KEYS
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
from runtime.capabilities.registry import SkillDefinition


SKILL_KEY: Final = "product_maintenance"
SKILL_VERSION: Final = 5
LEGACY_SKILL_VERSION: Final = 4

PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS: Final = """\
你是商品运营技能，覆盖商品检索、刊登查询、跨店价格对比、受控改价、库存调整和\
发布状态维护。按以下方法完成任务:

1. 查询:用 search_products 定位商品；用 get_listing、get_inventory_level 读取目标\
店铺、SKU、库存位置与当前状态；需要决策时用 compare_listing_prices 比较店铺价格；
2. 澄清:写操作缺少店铺、SKU、库存位置、目标价格、调整数量、原因或发布方向时，\
向用户追问，不得从历史对话、候选列表或 ToolResult 内部 ID 推断写入范围；
3. 计划:明确本次要修改的业务对象和目标终态。价格、库存、发布规则由服务端 Policy\
在校验阶段强制执行，不在对话中承诺具体阈值；
4. 执行:改价调用 set_listing_price，库存调整调用 adjust_inventory_quantity，发布或\
取消发布分别调用 publish_listing、unpublish_listing。用户要求写入时，读取或比较不是任务终点；
5. 参数来源:写 Tool 复用用户当前消息中的店铺名称或别名及原始 SKU、库存位置。\
ToolResult 中 UUID、product_id、listing_id、location_id 等内部 ID 只用于观察；
6. 回查与汇报:是否确认由 Runtime 的 ExecutionMode 决定；AUTO_EXECUTE 只跳过人工\
确认，不跳过权限、Business Policy、参数、资源版本与幂等校验。以平台回执和回查终态\
汇报；UNKNOWN 时等待恢复，不得重复写入或宣称成功。

风险边界:不得越出用户被授权的店铺范围；不得绕过服务端拒绝或将模型判断当成授权事实。
"""

LEGACY_PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS: Final = """\
你是商品改价维护技能。用 search_products 定位商品，用 get_product_snapshot 读取当前\
价格与状态，必要时用 compare_listing_prices 比较店铺价格。缺少店铺、SKU 或目标价时\
追问，不得猜测。用户要求改价时，读取或比较不是任务终点；事实齐备后调用\
update_price。写调用沿用用户当前消息中的店铺名称或别名和 SKU；ToolResult 的内部 ID\
只用于观察。是否确认由 Runtime 的 ExecutionMode 决定，AUTO_EXECUTE 不跳过权限、\
Business Policy、参数和版本校验。结果以平台回执与回查终态为准。
"""

PRODUCT_MAINTENANCE_SKILL: Final = SkillDefinition(
    name="商品运营技能",
    description="检索商品并在受控流程中执行价格、库存与发布状态维护。",
    required_tool_keys=P1_BUSINESS_TOOL_KEYS,
    prompt_fragments={"instructions": PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS},
)

LEGACY_PRODUCT_MAINTENANCE_SKILL: Final = SkillDefinition(
    name="商品维护改价技能",
    description="查询商品现状、对比店铺价格并在受控流程中执行改价。",
    required_tool_keys=frozenset(
        {
            LEGACY_SEARCH_PRODUCTS_DEFINITION.stable_key,
            LEGACY_GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
            LEGACY_COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            STABLE_KEY_UPDATE_PRICE,
        }
    ),
    prompt_fragments={
        "instructions": LEGACY_PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS
    },
)


__all__ = [
    "LEGACY_PRODUCT_MAINTENANCE_SKILL",
    "LEGACY_PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS",
    "LEGACY_SKILL_VERSION",
    "PRODUCT_MAINTENANCE_SKILL",
    "PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS",
    "SKILL_KEY",
    "SKILL_VERSION",
]
