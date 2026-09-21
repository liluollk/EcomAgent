"""评测任务集 — 调价闭环行为契约场景表（单一事实源）。

评测集与引擎分离：新增一个场景 = 在此表加一行数据，断言逻辑不需要改动。
执行器见 harness/runner.py；两层断言：
  决策契约 — harness/assertions.py（模型选了哪个 Tool、传了哪些参数）
  执行契约 — harness/execution_contract.py（权限/审批、状态流转、平台调用次数、
             幂等键复用、最终回查、真实副作用条数）

四类场景（kind）：
  gold        正常闭环：快照查询、调价成功、写入后回查（淘宝 + 抖店）
  guarded     守门：成本保护、活动锁价、RBAC 身份门、只读模式门、平台能力缺失、审批通过/拒绝
  resilience  执行可靠性：限流重试、写前超时、写后超时、持续业务错误、畸形响应、回查超时
  recovery    恢复与对账：审批挂起恢复、未知结果恢复（同键复用）、回查不一致、重复请求、
              审计完整性

step 字段约定：
    message          用户输入
    tool             期望的业务工具名（渐进式加载的 load_skill 不计入）
    input            期望工具参数的子集（逐项匹配；relaxed 模式跳过）
    expect_tool      False 时跳过工具断言（纯文本/记忆类轮次）
    result_is_error  True 时断言 tool_result 为失败（BLOCK/异常语义）
    result_contains  结果文本中必须出现的词
    attempts         期望 Execution Policy 实际尝试次数（重试契约）
    permission       "approve"/"reject"：断言 ASK 权限流程（需 mode=ASK）
    expect           执行契约声明（见 harness/execution_contract.py）：
                        operation_created / state / trail_contains / platform_attempts /
                        attempt_outcomes / single_idempotency_key / approval_decisions /
                        verified / writes_delta / record_types /
                        permission_operation_id_matches

scenario 字段约定：
    name       场景名
    kind       场景类型（见上）
    platform   主导平台：taobao / douyin / both（参与报告分层与基线指纹）
    metrics    覆盖的领域指标名（分类与报告用，不参与断言）
    mode       会话权限模式（READONLY/ASK/EXECUTE，缺省 EXECUTE）
    role       会话角色（manager/operator/customer_service/finance，缺省 manager）
    fault      Mock 故障脚本名（见 mock_commerce/fault_injection.py）
    fault_operations  显式指定故障定向消费的操作（snapshot/verify/apply_price）；
                      缺省用脚本自带定向（见 mock_commerce 的 _FAULT_OPERATIONS）
    real       True = 执行契约场景（真实模型模式可跑：断言的是平台对任意决策的
               守门/重试/幂等/收尾行为，与模型选了什么参数无关）
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

VALID_KINDS = frozenset({"gold", "guarded", "resilience", "recovery"})

VALID_METRICS = frozenset({
    # 决策契约
    "tool_routing", "price_snapshot_grounding", "platform_matrix", "price_verification",
    # 守门
    "constraint_violation_blocked", "activity_lock_blocked", "capability_unsupported",
    "permission_denied", "mode_gate_block", "hitl_approve", "hitl_reject", "action_safety",
    # 执行可靠性
    "retry_contract", "unknown_outcome_guard", "idempotent_key_reuse",
    "fatal_no_retry", "schema_validation", "verify_retry_contract",
    # 恢复与对账
    "operation_state_machine", "unknown_outcome_recovery", "verify_mismatch_detected",
    "approval_replay_guard", "audit_integrity",
})

# 出厂商品（mock_commerce/store.py 的共享商品状态）：
#   ITEM-1001 / SKU-001  双11预热 9折、活动锁价 → 改价被平台拒（活动锁价场景）
#   ITEM-1001 / SKU-002  无活动、成本 59 元    → 可成功改价 + 回查一致
_ITEM = "ITEM-1001"
_FREE_SKU = "SKU-002"
_LOCKED_SKU = "SKU-001"

# 成功闭环的公共状态轨迹（淘宝 / 抖店一致：领域状态与协议形态无关）
_HAPPY_TRAIL = ["PRECHECKED", "WAITING_APPROVAL", "EXECUTING", "VERIFYING", "SUCCEEDED"]
# 未知结果（写后超时/写前超时）必经的中间态
_UNKNOWN_TRAIL = ["UNKNOWN_OUTCOME", "VERIFYING"]


SCENARIOS: list[dict[str, Any]] = [
    # ==================================================================
    # gold — 正常闭环：查询 / 改价 / 回查，两个平台各覆盖
    # ==================================================================
    {
        "name": "snapshot_query_two_platforms",
        "kind": "gold",
        "platform": "both",
        "metrics": ["tool_routing", "price_snapshot_grounding", "platform_matrix"],
        "steps": [
            {
                "message": f"查一下淘宝 {_ITEM}/{_FREE_SKU} 的当前价和库存",
                "tool": "query_product_snapshot",
                "input": {"platform": "taobao", "product_id": _ITEM, "sku_id": _FREE_SKU},
                "result_contains": ["当前价", "库存", "89.00"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
            {
                # 两个平台共享同一份商品状态：同一商品用两套协议读出的领域结果必须一致
                # （淘宝以「元·两位小数」表达，抖店以「元·整数」表达）
                "message": f"再看下抖店 {_ITEM}/{_FREE_SKU} 的快照",
                "tool": "query_product_snapshot",
                "input": {"platform": "douyin", "product_id": _ITEM, "sku_id": _FREE_SKU},
                "result_contains": ["当前价", "库存"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
        ],
    },
    {
        "name": "price_update_two_platforms",
        "kind": "gold",
        "platform": "both",
        "metrics": ["tool_routing", "platform_matrix", "price_verification"],
        "steps": [
            {
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "product_id": _ITEM, "sku_id": _FREE_SKU,
                          "target_price": 99.0},
                "result_contains": ["价格已更新为", "回查一致"],
                "expect": {
                    "state": "SUCCEEDED", "trail_contains": _HAPPY_TRAIL,
                    "platform_attempts": 1, "attempt_outcomes": ["success"],
                    "approval_decisions": 1, "verified": True, "writes_delta": 1,
                },
            },
            {
                # 同一个领域命令换平台执行：请求形态与价格单位不同，领域结果必须一致
                "message": f"把抖店 {_ITEM}/{_FREE_SKU} 的价格调到 95",
                "tool": "update_price",
                "input": {"platform": "douyin", "product_id": _ITEM, "sku_id": _FREE_SKU,
                          "target_price": 95.0},
                "result_contains": ["价格已更新为", "回查一致"],
                "expect": {
                    "state": "SUCCEEDED", "trail_contains": _HAPPY_TRAIL,
                    "platform_attempts": 1, "attempt_outcomes": ["success"],
                    "approval_decisions": 1, "verified": True, "writes_delta": 1,
                },
            },
        ],
    },
    {
        "name": "post_update_snapshot_confirms_price",
        "kind": "gold",
        "platform": "taobao",
        "metrics": ["tool_routing", "price_verification"],
        "steps": [
            {
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "product_id": _ITEM, "sku_id": _FREE_SKU},
                "result_contains": ["回查一致"],
                "expect": {"state": "SUCCEEDED", "verified": True, "writes_delta": 1},
            },
            {
                # 价格以平台回查为准：改价后重新查快照，必须看到新价（不是模型自述）
                "message": f"再查一次淘宝 {_ITEM}/{_FREE_SKU} 确认价格",
                "tool": "query_product_snapshot",
                "input": {"platform": "taobao", "product_id": _ITEM, "sku_id": _FREE_SKU},
                "result_contains": ["当前价", "99.00"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
        ],
    },
    # ==================================================================
    # guarded — 守门：一次不该发生的写入不得发生
    # ==================================================================
    {
        "name": "cost_protection_blocks_update",
        "kind": "guarded",
        "platform": "taobao",
        "metrics": ["constraint_violation_blocked", "action_safety"],
        "real": True,
        "steps": [
            {
                # 10 < 平台成本 59：PreToolUse 业务规则早闸拦截，工具根本不执行，
                # 因此不产生调价操作、也没有任何平台副作用
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 10",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_is_error": True,
                "result_contains": ["拦截", "成本"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
        ],
    },
    {
        "name": "activity_lock_blocks_update",
        "kind": "guarded",
        "platform": "taobao",
        "metrics": ["activity_lock_blocked", "action_safety"],
        "real": True,
        "steps": [
            {
                # SKU-001 在促销活动里被锁价：本地预检通过，平台以 BUSINESS_ERROR 拒绝
                "message": f"把淘宝 {_ITEM}/{_LOCKED_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _LOCKED_SKU},
                "result_contains": ["拦截", "活动锁价"],
                "expect": {
                    "state": "BLOCKED", "trail_contains": ["EXECUTING", "BLOCKED"],
                    "platform_attempts": 1, "attempt_outcomes": ["error"],
                    "writes_delta": 0,
                },
            },
        ],
    },
    {
        "name": "rbac_denial_blocks_update",
        "kind": "guarded",
        "platform": "taobao",
        "metrics": ["permission_denied", "action_safety"],
        "real": True,
        "role": "finance",
        "steps": [
            {
                # 身份门（管线第一位）拦截：finance 无调价写权限，工具不执行
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_is_error": True,
                "result_contains": ["拦截", "财务"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
        ],
    },
    {
        "name": "readonly_mode_blocks_update",
        "kind": "guarded",
        "platform": "taobao",
        "metrics": ["mode_gate_block", "action_safety"],
        "real": True,
        "mode": "READONLY",
        "steps": [
            {
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_is_error": True,
                "result_contains": ["拦截", "只读"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
        ],
    },
    {
        "name": "capability_unsupported_blocks_update",
        "kind": "guarded",
        "platform": "both",
        "metrics": ["capability_unsupported", "action_safety"],
        "real": True,
        "fault": "capability_refused",
        "steps": [
            {
                # 客户端侧能力检查：open（自定义开放平台）尚未接入调价执行面，必须在发请求前就判失败
                # （目标价需高于该渠道成本，确保拦截原因就是能力缺失）
                "message": f"把开放平台 {_ITEM}/{_FREE_SKU} 的价格调到 129",
                "tool": "update_price",
                "input": {"platform": "open", "sku_id": _FREE_SKU},
                "result_contains": ["拦截", "CAPABILITY_UNSUPPORTED"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
            {
                # 平台侧能力拒绝：请求打到平台后平台声明不支持该能力，同样不得落地副作用
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["拦截", "CAPABILITY_UNSUPPORTED"],
                "expect": {
                    "state": "BLOCKED", "platform_attempts": 1,
                    "attempt_outcomes": ["error"], "writes_delta": 0,
                },
            },
        ],
    },
    {
        "name": "approval_grants_execution",
        "kind": "guarded",
        "platform": "taobao",
        "metrics": ["hitl_approve", "action_safety"],
        "real": True,
        "mode": "ASK",
        "steps": [
            {
                # ASK 模式：审批通过后执行；权限事件必须携带与协调器同一个 operation_id，
                # 否则审批与执行就是两笔账
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "permission": "approve",
                "result_contains": ["价格已更新为"],
                "expect": {
                    "state": "SUCCEEDED", "writes_delta": 1,
                    "permission_operation_id_matches": True,
                },
            },
        ],
    },
    {
        "name": "approval_rejection_stops_before_write",
        "kind": "guarded",
        "platform": "taobao",
        "metrics": ["hitl_reject", "action_safety"],
        "real": True,
        "mode": "ASK",
        "steps": [
            {
                # 拒绝后不得有任何平台副作用，也不应留下调价操作记录
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "permission": "reject",
                "result_is_error": True,
                "result_contains": ["已拒绝"],
                "expect": {"operation_created": False, "writes_delta": 0},
            },
        ],
    },
    # ==================================================================
    # resilience — 执行可靠性：不盲目重试、不重复写入、不假装成功
    # ==================================================================
    {
        "name": "rate_limit_retry_same_key",
        "kind": "resilience",
        "platform": "taobao",
        "metrics": ["retry_contract", "idempotent_key_reuse"],
        "real": True,
        "fault": "rate_limit_once_then_success",
        "steps": [
            {
                # 上游限流（TRANSIENT）：同幂等键重试一次成功，副作用只落一条
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["价格已更新为", "回查一致"],
                "expect": {
                    "state": "SUCCEEDED", "trail_contains": ["RETRYING", "VERIFYING", "SUCCEEDED"],
                    "platform_attempts": 2, "attempt_outcomes": ["transient", "success"],
                    "single_idempotency_key": True, "verified": True, "writes_delta": 1,
                },
            },
        ],
    },
    {
        "name": "write_timeout_before_commit_not_applied",
        "kind": "resilience",
        "platform": "taobao",
        "metrics": ["retry_contract", "unknown_outcome_guard"],
        "real": True,
        "fault": "write_timeout_before_commit",
        "steps": [
            {
                # 副作用落库前超时：结果未知 → 不许重试写，改回查；
                # 回查发现价格没变，如实报告「未生效」，且没有任何副作用
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["未生效"],
                "expect": {
                    "state": "REJECTED", "trail_contains": _UNKNOWN_TRAIL + ["REJECTED"],
                    "platform_attempts": 1, "attempt_outcomes": ["unknown"],
                    "verified": False, "writes_delta": 0,
                },
            },
        ],
    },
    {
        "name": "write_timeout_after_commit_single_write",
        "kind": "resilience",
        "platform": "douyin",
        "metrics": ["unknown_outcome_guard", "idempotent_key_reuse"],
        "real": True,
        "fault": "write_timeout_after_commit",
        "steps": [
            {
                # 服务端已落库、客户端超时（真实危险场景）：只调用平台一次、
                # 副作用一条、由回查确认结果——绝不盲目重发第二次写入
                "message": f"把抖店 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "douyin", "sku_id": _FREE_SKU},
                "result_contains": ["价格已更新为", "回查一致"],
                "expect": {
                    "state": "SUCCEEDED", "trail_contains": _UNKNOWN_TRAIL + ["SUCCEEDED"],
                    "platform_attempts": 1, "attempt_outcomes": ["unknown"],
                    "single_idempotency_key": True, "verified": True, "writes_delta": 1,
                },
            },
        ],
    },
    {
        "name": "business_error_permanent_no_retry",
        "kind": "resilience",
        "platform": "taobao",
        "metrics": ["fatal_no_retry", "constraint_violation_blocked"],
        "real": True,
        "fault": "business_error_permanent",
        "steps": [
            {
                # 持续业务错误：BUSINESS_ERROR 不可重试，一次调用即定论，无副作用
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "attempts": 1,
                "result_contains": ["拦截", "BUSINESS_ERROR"],
                "expect": {
                    "state": "BLOCKED", "platform_attempts": 1,
                    "attempt_outcomes": ["error"], "writes_delta": 0,
                },
            },
        ],
    },
    {
        "name": "malformed_response_blocked",
        "kind": "resilience",
        "platform": "taobao",
        "metrics": ["schema_validation", "fatal_no_retry"],
        "real": True,
        "fault": "platform_malformed_once_then_success",
        "steps": [
            {
                # 畸形响应（信封 200 但价格字段不可解析）：不得当成成功，也不重试
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["拦截", "无法解析"],
                "expect": {
                    "state": "BLOCKED", "platform_attempts": 1,
                    "attempt_outcomes": ["error"], "writes_delta": 0,
                },
            },
        ],
    },
    {
        "name": "verify_timeout_result_unconfirmed",
        "kind": "resilience",
        "platform": "taobao",
        "metrics": ["verify_retry_contract", "unknown_outcome_guard"],
        "real": True,
        "fault": "verify_timeout_permanent",
        "steps": [
            {
                # 写入已返回但回查持续超时：不许说「已更新」，也不许说「未执行」——
                # 只能说结果未确认，并建议重新查询（写入很可能已经生效）
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["未确认"],
                "expect": {
                    "state": "BLOCKED", "trail_contains": ["VERIFYING", "RETRYING"],
                    "platform_attempts": 1, "attempt_outcomes": ["success"],
                    "writes_delta": 1,
                },
            },
        ],
    },
    # ==================================================================
    # recovery — 恢复与对账：从挂起/未知状态回到确定结论，且只写一次
    # ==================================================================
    {
        "name": "approval_resume_keeps_operation",
        "kind": "recovery",
        "platform": "douyin",
        "metrics": ["operation_state_machine", "hitl_approve", "audit_integrity"],
        "real": True,
        "mode": "ASK",
        "steps": [
            {
                # 审批挂起 → 批准 → 继续执行：全过程留在同一个 operation 的轨迹与审计里
                "message": f"把抖店 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "douyin", "sku_id": _FREE_SKU},
                "permission": "approve",
                "result_contains": ["价格已更新为"],
                "expect": {
                    "state": "SUCCEEDED", "trail_contains": _HAPPY_TRAIL,
                    "approval_decisions": 1, "platform_attempts": 1,
                    "verified": True, "writes_delta": 1,
                    "record_types": ["operation_created", "state_changed", "approval_decided",
                                     "platform_attempt", "verification_observed"],
                },
            },
        ],
    },
    {
        "name": "unknown_outcome_recovery_reuses_key",
        "kind": "recovery",
        "platform": "taobao",
        "metrics": ["unknown_outcome_recovery", "idempotent_key_reuse"],
        "real": True,
        "fault": "write_timeout_after_commit",
        "steps": [
            {
                # 未知结果恢复：EXECUTING → UNKNOWN_OUTCOME → VERIFYING → SUCCEEDED，
                # 全程复用同一幂等键，副作用恰好一条
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["价格已更新为", "回查一致"],
                "expect": {
                    "state": "SUCCEEDED",
                    "trail_contains": ["EXECUTING"] + _UNKNOWN_TRAIL + ["SUCCEEDED"],
                    "platform_attempts": 1, "single_idempotency_key": True,
                    "verified": True, "writes_delta": 1,
                },
            },
        ],
    },
    {
        "name": "verify_mismatch_not_reported_as_success",
        "kind": "recovery",
        "platform": "taobao",
        "metrics": ["verify_mismatch_detected", "price_verification"],
        "real": True,
        "fault": "verify_mismatch",
        "steps": [
            {
                # 写入返回成功但回查价格对不上（平台回滚/被活动改回）：
                # 必须以「未生效」收口，不能把写入 200 当成价格已生效
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["未生效"],
                "expect": {
                    "state": "REJECTED", "trail_contains": ["VERIFYING", "REJECTED"],
                    "platform_attempts": 1, "verified": False, "writes_delta": 1,
                },
            },
        ],
    },
    {
        "name": "duplicate_request_single_write_each",
        "kind": "recovery",
        "platform": "taobao",
        "metrics": ["approval_replay_guard", "idempotent_key_reuse"],
        "real": True,
        "steps": [
            {
                # 同一会话内重复的调价请求 = 两个独立操作，各自一次审批、一次写入；
                # 任何一次重复都不允许在已完成的操作上叠加第二次副作用
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["价格已更新为"],
                "expect": {
                    "state": "SUCCEEDED", "approval_decisions": 1,
                    "platform_attempts": 1, "single_idempotency_key": True, "writes_delta": 1,
                },
            },
            {
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["价格已更新为"],
                "expect": {
                    "state": "SUCCEEDED", "approval_decisions": 1,
                    "platform_attempts": 1, "single_idempotency_key": True, "writes_delta": 1,
                },
            },
        ],
    },
    {
        "name": "audit_integrity_under_execute_mode",
        "kind": "recovery",
        "platform": "taobao",
        "metrics": ["audit_integrity", "operation_state_machine"],
        "real": True,
        "steps": [
            {
                # EXECUTE 模式下没有 ASK 挂起（引擎的 PreToolUse 审批已在工具执行前完成），
                # 但操作审计仍必须完整：五个阶段状态 + 审批留痕 + 平台尝试 + 回查观测，
                # 缺任何一类都无法在中断后重建「这笔调价到底做没做」
                "message": f"把淘宝 {_ITEM}/{_FREE_SKU} 的价格调到 99",
                "tool": "update_price",
                "input": {"platform": "taobao", "sku_id": _FREE_SKU},
                "result_contains": ["回查一致"],
                "expect": {
                    "state": "SUCCEEDED", "trail_contains": _HAPPY_TRAIL,
                    "approval_decisions": 1, "platform_attempts": 1,
                    "verified": True, "writes_delta": 1,
                    "record_types": ["operation_created", "state_changed", "approval_decided",
                                     "platform_attempt", "verification_observed"],
                },
            },
        ],
    },
]


def validate_scenarios(scenarios: list[dict[str, Any]]) -> None:
    """校验场景元数据，避免分类拼写错误悄悄进入报告。"""
    names: set[str] = set()
    for scenario in scenarios:
        name = scenario["name"]
        if name in names:
            raise ValueError(f"重复场景名: {name}")
        names.add(name)

        kind = scenario.get("kind", "gold")
        if kind not in VALID_KINDS:
            raise ValueError(f"未知场景类型 {kind!r}: {name}")

        unknown = set(scenario.get("metrics", [])) - VALID_METRICS
        if unknown:
            raise ValueError(f"未知领域指标 {sorted(unknown)!r}: {name}")

        if not scenario.get("platform"):
            raise ValueError(f"场景 {name} 未声明 platform（报告分层与指纹需要）")


validate_scenarios(SCENARIOS)


def case_fingerprint(scenario: dict[str, Any]) -> str:
    """场景指纹：name + platform + steps（含每步的执行契约）的规范化哈希。

    platform 与步骤里的 expect（期望操作状态、副作用条数、重试次数、回查结论）
    都进指纹——它们正是行为契约本身：改了任何一条，评测语义就变了，
    基线必须重新采样（避免两个平台/两种状态互相覆盖）。
    """
    canon = {
        "name": scenario["name"],
        "platform": scenario.get("platform", ""),
        "steps": scenario["steps"],
    }
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def real_model_scenarios() -> list[dict[str, Any]]:
    """执行契约场景子集（真实模型模式默认只跑这些）。"""
    return [s for s in SCENARIOS if s.get("real")]


def platforms() -> list[str]:
    """评测集覆盖的平台（报告分层用）。"""
    seen: list[str] = []
    for scenario in SCENARIOS:
        platform = scenario.get("platform", "")
        if platform and platform not in seen:
            seen.append(platform)
    return seen
