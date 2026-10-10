"""``product_maintenance`` Skill 数据化内容(计划 03 Task 6;架构文档 §8)。

首版只有一个发布的改价流程技能:说明任务方法、可用 Tool 与风险边界。
它不重复存储商品事实、价格规则或 SOP 正文——当前商品与价格通过 Tool
读取,硬性业务规则通过服务端 Policy 读取,本 Skill 只描述"怎么做"。

按架构文档 §5/§6,指令文本不包含任何具体阈值数字(最低允许价、单次
变动上限、示例现价等);数字事实一律由服务端在写前校验。
"""

from __future__ import annotations

from typing import Final

from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import STABLE_KEY_UPDATE_PRICE
from runtime.capabilities.registry import SkillDefinition


SKILL_KEY: Final = "product_maintenance"
SKILL_VERSION: Final = 4

PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS: Final = """\
你是商品改价维护技能。按以下任务方法完成改价请求:

1. 查询:用 search_products 定位用户所说的商品,用 get_product_snapshot \
读取目标店铺与 SKU 的当前价格、币种与状态,必要时用 compare_listing_prices \
对比各店铺价格后再给出建议;
2. 对比:把用户给出的目标价与当前价格、币种、店铺范围核对清楚;缺失或\
有歧义的信息(店铺、SKU、目标价)主动向用户追问,不得猜测店铺或编造数据;
3. 计划:明确"哪个店铺、哪个 SKU、改成什么价格"的计划;改价的合规性\
(价格下限、活动锁价、单次变动限制等)由服务端策略在校验阶段强制执行,\
不要在对话中替平台承诺或解释具体规则数值。用户要求改价时,读取或比较不是任务终点;\
事实齐备后继续调用 update_price,不要只返回建议;
4. 参数来源:后续写调用使用用户当前消息中的店铺名称或别名及 SKU。ToolResult\
中的 UUID、product_id、listing_id 等内部 ID 只用于观察,不得替代用户明确给出的\
写入范围;
5. 执行与回查:是否需要用户确认由 Runtime 根据当前 Workspace 的\
ExecutionMode 决定;AUTO_EXECUTE 只跳过人工确认,不跳过权限、Business Policy、\
参数和资源版本校验。以平台返回的最终状态(写入前后价格、\
版本与回查结论)为准汇报结果,不要凭模型文本宣称已完成。

风险边界:绝不越出用户被授权的店铺范围;写操作结果不确定(超时或响应丢失)\
时等待系统回查结论,不要重复发起写操作。
"""


PRODUCT_MAINTENANCE_SKILL: Final = SkillDefinition(
    name="商品维护改价技能",
    description="查询商品现状、对比店铺价格并在受控流程中执行改价。",
    required_tool_keys=frozenset(
        {
            SEARCH_PRODUCTS_DEFINITION.stable_key,
            GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
            COMPARE_LISTING_PRICES_DEFINITION.stable_key,
            STABLE_KEY_UPDATE_PRICE,
        }
    ),
    prompt_fragments={"instructions": PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS},
)


__all__ = [
    "PRODUCT_MAINTENANCE_SKILL",
    "PRODUCT_MAINTENANCE_SKILL_INSTRUCTIONS",
    "SKILL_KEY",
    "SKILL_VERSION",
]
