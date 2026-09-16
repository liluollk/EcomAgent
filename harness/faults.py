"""评测故障装配 — 场景级确定性故障脚本 + 快执行策略。

runner（python -m harness）与 e2e 驱动器（tests/e2e）共用本模块，
保证「场景数据 → Runner → Mock API Fault Script → Agent 编排 → Event →
Assertion」是一条链路上的同一套语义。

快策略要点（与生产默认的差异，仅评测进程内生效）：
  - 退避基数 0.05s（默认 0.5s）：重试场景不拖慢评测；
  - 读超时 0.5s / 写超时 0.8s：配合故障脚本 1.2s 睡眠，确定性触发
    客户端超时（服务端先落副作用、后挂起，复现真实危险场景）。

故障定向消费（调价闭环的关键）：
  调价路径的快照、回查、写入**都是 POST**（淘宝单端点按信封 method 分发、
  抖店是 JSON body），按 HTTP 方法区分不了读写——若故障不限目标操作，
  场景里第一次快照预检就会把唯一的写故障步吃掉，重试 / 超时契约全成假阴性。

  因此定向改为**按操作语义**判定，事实源在两处：
    1. 脚本自带目标操作（mock_commerce.fault_injection._FAULT_OPERATIONS，
       如 write_timeout_* → apply_price、verify_* → verify）；
    2. 场景可用 fault_operations 字段显式覆盖（例如把通用脚本钉在 snapshot 上）。
  Adapter 在发起请求前把操作语义写进 mock 的 contextvar，mock 侧据此决定
  这一请求该不该消费故障步。
"""

from __future__ import annotations

from typing import Any

from execution.policy import ExecutionPolicy, configure_default_policy
from execution.policy.retry import RetryConfig, RetryPolicy
from execution.policy.timeout import TimeoutConfig, TimeoutPolicy
from mock_commerce import fault_injection
from mock_commerce.domain import reset_idempotency
from mock_commerce.store import reset_product_state, reset_writes

# 必须大于快策略的写超时（0.8s），客户端才会真正等到超时
FAULT_SLEEP_SECONDS = 1.2


def _fast_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        retry=RetryPolicy(
            RetryConfig(max_retries=2, base_delay=0.05, max_delay=0.2, backoff_multiplier=2.0)
        ),
        timeout=TimeoutPolicy(
            TimeoutConfig(default_seconds=5.0, read_seconds=0.5, write_seconds=0.8)
        ),
    )


def _fault_target_operations(scenario: dict[str, Any]) -> set[str] | None:
    """故障脚本定向消费的目标操作集合（None = 用脚本自带定向 / 不限）。

    优先级：场景显式 fault_operations > 脚本自带（_FAULT_OPERATIONS）> 不限。
    通用脚本（timeout_once_then_success 等）本来就没有操作语义，需要定向时由
    场景显式声明；这里不做「按工具名猜」的推断，避免把猜测藏进装配层。
    """
    declared = scenario.get("fault_operations")
    if declared is not None:
        return set(declared)
    return None


def activate_scenario_faults(scenario: dict[str, Any]) -> None:
    """按场景表装载故障脚本并切换快执行策略（幂等：重复调用安全）。

    无论场景是否声明 fault，都重置故障 / 幂等 / 写日志 / 商品价格状态并启用
    快策略——保证相邻场景零状态串扰（改价会把商品价格写进共享状态，不重置
    会让后续场景看到上一个场景的价格）。
    """
    reset_writes()
    reset_idempotency()
    reset_product_state()
    fault_injection.reset_fault()
    fault = scenario.get("fault")
    if fault:
        fault_injection.load_script(
            fault,
            timeout_seconds=FAULT_SLEEP_SECONDS,
            operations=_fault_target_operations(scenario),
        )
    configure_default_policy(_fast_policy())


def deactivate_scenario_faults() -> None:
    """恢复生产默认策略与无故障状态（评测结束/测试 teardown 用）。"""
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    reset_product_state()
    configure_default_policy(ExecutionPolicy())
