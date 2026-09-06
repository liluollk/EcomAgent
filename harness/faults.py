"""评测故障装配 — 场景级确定性故障脚本 + 快执行策略。

runner（python -m harness）与 e2e 驱动器（tests/e2e）共用本模块，
保证「场景数据 → Runner → Mock API Fault Script → Runtime → Event →
Assertion」是一条链路上的同一套语义。

快策略要点（与生产默认的差异，仅评测进程内生效）：
  - 退避基数 0.05s（默认 0.5s）：重试场景不拖慢评测；
  - 读超时 0.5s / 写超时 0.8s：配合故障脚本 1.2s 睡眠，确定性触发
    客户端超时（服务端先落副作用、后挂起，复现真实危险场景）。
"""

from __future__ import annotations

from typing import Any

from execution.policy import ExecutionPolicy, configure_default_policy
from execution.policy.retry import RetryConfig, RetryPolicy
from execution.policy.timeout import TimeoutConfig, TimeoutPolicy
from mock_commerce import fault_injection
from mock_commerce.domain import reset_idempotency
from mock_commerce.store import reset_writes

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


def activate_scenario_faults(scenario: dict[str, Any]) -> None:
    """按场景表装载故障脚本并切换快执行策略（幂等：重复调用安全）。

    无论场景是否声明 fault，都重置故障/幂等/写日志并启用快策略——
    保证相邻场景零状态串扰。
    """
    reset_writes()
    reset_idempotency()
    fault_injection.reset_fault()
    fault = scenario.get("fault")
    if fault:
        fault_injection.load_script(fault, timeout_seconds=FAULT_SLEEP_SECONDS)
    configure_default_policy(_fast_policy())


def deactivate_scenario_faults() -> None:
    """恢复生产默认策略与无故障状态（评测结束/测试 teardown 用）。"""
    fault_injection.reset_fault()
    reset_writes()
    reset_idempotency()
    configure_default_policy(ExecutionPolicy())
