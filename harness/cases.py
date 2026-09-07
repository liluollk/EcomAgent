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
    result_contains  结果文本中必须出现的词（成功/失败一视同仁）
    attempts         期望 Execution Policy 实际尝试次数（重试契约）
    permission       "approve"/"reject"：断言 ASK 权限流程（需 mode=ASK）
scenario 字段约定：
    name      场景名
    setup     预先准备钩子名（见 harness/hooks.py）
    after     收尾验证钩子名（见 harness/hooks.py）
    mode      会话权限模式（READONLY/ASK/EXECUTE，缺省 EXECUTE）
    role      会话角色（manager/operator/customer_service/finance，缺省 manager）
    fault     Mock Commerce API 确定性故障脚本名（见 mock_commerce/fault_injection.py）
    real      True = 运行时契约场景（真实模型模式可跑：断言的是运行时对任意
              决策的守门/重试/幂等/收尾行为，与模型选了哪个工具的具体参数无关）
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
        "real": True,
        "steps": [
            # 20 < 成本 59：早道 PreToolUse 业务规则拦截（成本价由平台真相 lookup 提供，
            # 模型不传 cost_price；真实模型漏报/谎报成本也由规则 + 平台 409 双闸兜底），
            # 两条路径文本公共 token 为「成本」「低于」
            {"message": "把淘宝 SKU-001 价格调到 20", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "result_is_error": True,
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
    "real": True,
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
    {
        "name": "shelf_toggle",
    "real": True,
        "steps": [
            {"message": "把淘宝 SKU-001 下架", "tool": "product_shelf",
             "input": {"channel": "taobao", "sku": "SKU-001", "action": "off"}},
            {"message": "把淘宝 SKU-001 重新上架", "tool": "product_shelf",
             "input": {"channel": "taobao", "sku": "SKU-001", "action": "on"}},
        ],
    },
    {
        "name": "promotion_query_and_create",
    "real": True,
        "steps": [
            {"message": "看下抖音进行中的促销", "tool": "query_promotions",
             "input": {"channel": "douyin"}},
            {"message": "给抖音 SKU-003 建个促销活动", "tool": "create_promotion",
             "input": {"channel": "douyin", "sku": "SKU-003"}},
        ],
    },
    {
        "name": "business_review",
    "real": True,
        "steps": [
            {"message": "看下淘宝近7天的销售分析", "tool": "query_order_stats",
             "input": {"channel": "taobao", "period": "近7天"}},
            {"message": "看下淘宝的售后统计", "tool": "query_after_sales_stats",
             "input": {"channel": "taobao"}},
        ],
    },
    {
        "name": "anomaly_watch",
    "real": True,
        "steps": [
            {"message": "看下抖音有什么异常预警", "tool": "query_anomalies",
             "input": {"channel": "douyin"}},
        ],
    },
    {
        "name": "unknown_sku_graceful",
    "real": True,
        "steps": [
            # 平台对未知 SKU 返回空库存行：工具优雅返回（库存 0），不报错不崩溃
            {"message": "查一下淘宝 SKU-999 的库存", "tool": "query_inventory",
             "input": {"channel": "taobao", "sku": "SKU-999"}},
        ],
    },
    {
        "name": "readonly_blocks_write",
    "real": True,
        "mode": "READONLY",
        "steps": [
            # 模式门拦截：只读模式下写操作被 BLOCK（reason 含「只读」）
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "result_is_error": True,
             "result_contains": ["拦截", "只读"]},
        ],
    },
    {
        "name": "rbac_denial",
    "real": True,
        "role": "finance",
        "steps": [
            # 身份门拦截：finance 角色无 update_price 写权限，管线第一位即 BLOCK
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "result_is_error": True,
             "result_contains": ["拦截", "财务"]},
        ],
    },
    {
        "name": "memory_forget",
    "real": True,
        "after": "assert_memory_forgotten",
        "steps": [
            {"message": "请记住：双11 备货 3 万件", "expect_tool": False},
            {"message": "忘记 双11", "expect_tool": False},
        ],
    },
    # ------------------------------------------------------------------
    # 外部故障与执行可靠性（Evaluation 2.0）：确定性故障脚本 + 策略层契约
    # ------------------------------------------------------------------
    {
        # E14 上游限流一次：分类 TRANSIENT → 同幂等键重试成功
        "name": "upstream_429_retry_success",
    "real": True,
        "fault": "rate_limit_once_then_success",
        "steps": [
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "attempts": 2, "result_contains": ["已更新"]},
        ],
    },
    {
        # E15 上游超时一次（读操作）：客户端短超时 → TRANSIENT 重试成功
        "name": "upstream_timeout_retry_success",
    "real": True,
        "fault": "timeout_once_then_success",
        "steps": [
            {"message": "查一下淘宝 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "attempts": 2, "result_contains": ["库存"]},
        ],
    },
    {
        # E16 上游持续 500：FATAL 不重试，优雅返回平台错误文本
        "name": "upstream_permanent_failure",
    "real": True,
        "fault": "permanent_500",
        "steps": [
            {"message": "查一下淘宝 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "attempts": 1, "result_contains": ["平台错误"]},
        ],
    },
    {
        # E17 超时重试 + 幂等：服务端先落副作用再挂起（真实危险场景），
        # 重试同幂等键回放首次结果，写副作用只落一次（after 钩子断言）
        "name": "idempotent_repeated_write",
    "real": True,
        "fault": "timeout_once_then_success",
        "after": "assert_single_price_write",
        "steps": [
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "attempts": 2, "result_contains": ["已更新"]},
        ],
    },
    {
        # E18 上游畸形响应（HTTP 200 + code=0 但 data 缺字段/类型错）：
        # 结果校验层以 UPSTREAM_INVALID_RESPONSE 拦截，不重试、优雅降级
        "name": "malformed_upstream_response",
    "real": True,
        "fault": "malformed_once_then_success",
        "steps": [
            {"message": "查一下淘宝 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "attempts": 1, "result_contains": ["上游响应异常"]},
        ],
    },
    {
        # E19 部分工具失败不拖垮会话：第一步 500 优雅报错，第二步恢复正常
        "name": "partial_tool_failure",
    "real": True,
        "fault": "internal_error_once_then_success",
        "steps": [
            {"message": "查一下淘宝 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "attempts": 1, "result_contains": ["平台错误"]},
            {"message": "查一下京东 SKU-001 的库存", "tool": "query_inventory",
             "input": {"channel": "jd", "sku": "SKU-001"},
             "result_contains": ["库存"]},
        ],
    },
    {
        # E20 ASK 模式 + 批准：permission_request 后人工放行，工具真实执行
        "name": "permission_approve_then_execute",
    "real": True,
        "mode": "ASK",
        "steps": [
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "permission": "approve", "result_contains": ["已更新"]},
        ],
    },
    {
        # E21 ASK 模式 + 拒绝：拒绝后不执行，turn 仍以 complete 完整收尾
        "name": "permission_reject_then_stop",
    "real": True,
        "mode": "ASK",
        "steps": [
            {"message": "把淘宝 SKU-001 价格调到 89", "tool": "update_price",
             "input": {"channel": "taobao", "sku": "SKU-001"},
             "permission": "reject", "result_is_error": True,
             "result_contains": ["已拒绝"]},
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


def real_model_scenarios() -> list[dict[str, Any]]:
    """运行时契约场景子集（真实模型模式默认只跑这些）。"""
    return [s for s in SCENARIOS if s.get("real")]
