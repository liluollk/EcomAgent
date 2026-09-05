"""评测任务集 — 行为契约场景数据表（单一事实源）。

评测集与引擎分离：新增一个场景 = 在此表加一行数据，断言逻辑不需要改动。
执行器见 harness/runner.py 与 harness/assertions.py；
pytest 入口（tests/e2e/test_e2e_demo.py）与独立入口（python -m harness）
共用本表。

step 字段约定：
    message          用户输入
    tool             期望的业务工具名（渐进式加载的 load_skill 不计入）
    input            期望工具参数的子集（逐项匹配）
    expect_tool      False 时跳过工具断言（纯文本/记忆类轮次）
    result_is_error  True 时断言 tool_result 为失败（BLOCK/异常语义）
    result_contains  失败/成功结果文本中必须出现的词
scenario 字段约定：
    name      场景名
    setup     预先准备钩子名（见 harness/hooks.py）
    after     收尾验证钩子名（见 harness/hooks.py）
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "six_step_business_chain",
        "steps": [
            {"message": "查一下淘宝 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "taobao", "sku": "SKU-001"}},
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"}},
            {"message": "给抖音 SKU-003 建个促销活动", "tool": "create_promotion",
             "input": {"channel": "douyin", "sku": "SKU-003"}},
            {"message": "查一下淘宝订单 TB-10086 的状态", "tool": "query_order_status",
             "input": {"channel": "taobao", "order_id": "TB-10086"}},
            {"message": "淘宝订单 TB-10086 有个客诉，建工单", "tool": "service_ticket",
             "input": {"channel": "taobao", "order_id": "TB-10086"}},
            {"message": "查一下上架规范", "tool": "query_knowledge_base",
             "input": {"topic": "上架规范"}},
        ],
    },
    {
        "name": "cost_interception",
        "steps": [
            {"message": "把淘宝 SKU-001 价格调到 20",  # 20 < 成本 59
             # mock 剧本模拟「底层规则拒绝」：直接产出失败结果，无业务 tool_start；
             # 真实 PreToolUse BLOCK 路径（有 tool_start）由 permission 单元测试覆盖
             "expect_tool": False, "result_is_error": True,
             "result_contains": ["成本", "低于"]},
        ],
    },
    {
        "name": "dynamic_channel_add",
        "setup": "add_pdd_channel",
        "steps": [
            {"message": "查一下 pdd 渠道 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "pdd"}},
        ],
    },
    {
        "name": "cross_session_memory",
        "after": "assert_memory_landed",
        "steps": [
            {"message": "请记住：双11 备货 3 万件", "expect_tool": False},
        ],
    },
    {
        "name": "skill_creation",
        "after": "assert_skill_created",
        "steps": [
            {"message": "把查库存的流程做成一个叫 stock_check_pro 的技能", "tool": "save_skill",
             "input": {"name": "stock_check_pro"}},
        ],
    },
]


def case_fingerprint(scenario: dict[str, Any]) -> str:
    """场景指纹：name + steps 的规范化哈希。

    用于 baseline 对比时识别评测集的增删改：评测集一旦变化（哪怕改一步
    期望），指纹即变化，基线不再直接可比。
    """
    canon = {"name": scenario["name"], "steps": scenario["steps"]}
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]