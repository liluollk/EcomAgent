"""ExecutionPolicy 编排器测试 — 超时 → 分类重试 → 幂等键 → 校验 的组装语义。"""

import asyncio

import pytest

from execution.policy import (
    ExecutionOutcome,
    ExecutionPolicy,
    get_last_outcome,
    set_call_context,
)
from execution.policy.retry import RetryConfig, RetryPolicy
from execution.policy.timeout import TimeoutConfig, TimeoutPolicy
from execution.policy.validation import ValidationError


class FlakyUpstream:
    """前 fail_times 次抛瞬态异常，随后成功；记录每次收到的幂等键。"""

    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.keys: list = []
        self.fail_times = fail_times

    async def __call__(self, key):
        self.calls += 1
        self.keys.append(key)
        if self.calls <= self.fail_times:
            raise RateLimitedError()
        return {"sku": "SKU-1001", "discount": 0.8}


class RateLimitedError(Exception):
    code = 10005  # 平台限流 → TRANSIENT


class BusinessConflictError(Exception):
    code = 10004  # 业务冲突 → 不重试


@pytest.mark.asyncio
async def test_transient_error_retries_with_same_idempotency_key():
    """限流重试：多次尝试携带同一 X-Idempotency-Key（防止重试重复副作用）。"""
    set_call_context("s1", 17)
    policy = ExecutionPolicy(retry=RetryPolicy(RetryConfig(max_retries=3, base_delay=0.01)))
    flaky = FlakyUpstream(2)
    outcome = await policy.execute("create_promotion", flaky, params={"sku": "SKU-1001"})

    assert flaky.calls == 3
    assert outcome.attempts == 3
    assert len(set(flaky.keys)) == 1 and flaky.keys[0], "重试必须复用同一幂等键"
    assert "t17" in flaky.keys[0] and "create_promotion" in flaky.keys[0]
    assert outcome.idempotency_key == flaky.keys[0]
    assert get_last_outcome() is outcome


@pytest.mark.asyncio
async def test_business_error_not_retried():
    calls = {"n": 0}

    async def run(key):
        calls["n"] += 1
        raise BusinessConflictError("x")

    with pytest.raises(BusinessConflictError):
        await ExecutionPolicy().execute("update_price", run)
    assert calls["n"] == 1, "BUSINESS_ERROR 不应重试"


@pytest.mark.asyncio
async def test_timeout_wraps_each_attempt_and_retries():
    class SlowOnce:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self, key):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(5.0)
            return {"ok": True}

    policy = ExecutionPolicy(
        retry=RetryPolicy(RetryConfig(max_retries=2, base_delay=0.01)),
        timeout=TimeoutPolicy(TimeoutConfig(read_seconds=0.05, write_seconds=0.05)),
    )
    slow = SlowOnce()
    outcome = await policy.execute("query_inventory", slow)
    assert slow.calls == 2
    assert outcome.attempts == 2


@pytest.mark.asyncio
async def test_malformed_response_rejected_by_validation():
    async def run(key):
        return {"unexpected_field": True}  # HTTP 200 + code=0 但 data 畸形

    with pytest.raises(ValidationError) as ei:
        await ExecutionPolicy().execute("query_order_status", run)
    assert ei.value.code == "UPSTREAM_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_outcome_metadata_and_reads_skip_idempotency():
    async def run(key):
        assert key is None, "读操作不应携带幂等键"
        return {"status": "已发货"}

    outcome = await ExecutionPolicy().execute("query_order_status", run)
    assert isinstance(outcome, ExecutionOutcome)
    assert outcome.attempts == 1
    assert outcome.idempotency_key is None
    assert outcome.duration_ms >= 0
    assert outcome.idempotent_replay is False
