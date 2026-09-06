"""策略单元测试 — RetryPolicy / TimeoutPolicy / IdempotencyPolicy / ResultValidator。"""

import asyncio

import pytest

from execution.error_classification import ErrorCategory
from execution.policy.idempotency import IdempotencyPolicy
from execution.policy.retry import RetryConfig, RetryPolicy
from execution.policy.timeout import TimeoutConfig, TimeoutError, TimeoutPolicy
from execution.policy.validation import ResultValidator, ValidationError


def test_retry_should_retry_and_backoff():
    p = RetryPolicy(RetryConfig(max_retries=3, base_delay=0.5, max_delay=2.0, backoff_multiplier=2.0))
    assert p.should_retry(ErrorCategory.TRANSIENT_ERROR, 0)
    assert not p.should_retry(ErrorCategory.CLIENT_ERROR, 0)
    assert not p.should_retry(ErrorCategory.TRANSIENT_ERROR, 3)  # 次数耗尽
    assert p.delay_for(0) == 0.5
    assert p.delay_for(1) == 1.0
    assert p.delay_for(5) == 2.0  # 封顶 max_delay


@pytest.mark.asyncio
async def test_timeout_read_write_split():
    p = TimeoutPolicy(TimeoutConfig(read_seconds=1.0, write_seconds=2.0))
    assert p.timeout_for("query_inventory") == 1.0
    assert p.timeout_for("update_price") == 2.0

    async def slow():
        await asyncio.sleep(1.0)

    with pytest.raises(TimeoutError) as ei:
        await p.execute("query_inventory", slow)
    assert ei.value.timeout == 1.0
    assert "超时" in str(ei.value)


def test_idempotency_key_shape_and_write_only():
    p = IdempotencyPolicy()
    k1 = p.generate_key("sess1", 3, "update_price", {"sku": "SKU-1"})
    k2 = p.generate_key("sess1", 3, "update_price", {"sku": "SKU-1"})
    assert k1 == k2, "同上下文同参数应生成同一幂等键"
    assert k1.startswith("idem_sess1_t3_update_price_")
    assert p.generate_key("sess1", 4, "update_price", {"sku": "SKU-1"}) != k1
    assert p.generate_key("sess1", 3, "update_price", {"sku": "SKU-2"}) != k1
    assert p.should_attach("update_price")
    assert not p.should_attach("query_inventory")
    assert not p.should_attach("load_skill")


def test_validation_schema_and_business():
    v = ResultValidator()
    v.validate("update_price", {"sku": "SKU-1", "new_price": 89.0})
    with pytest.raises(ValidationError) as ei:
        v.validate("update_price", {"sku": "SKU-1"})
    assert ei.value.code == "UPSTREAM_INVALID_RESPONSE"
    with pytest.raises(ValidationError):
        v.validate("update_price", {"sku": "SKU-1", "new_price": "贵"})
    with pytest.raises(ValidationError) as ei:
        v.validate_business("update_price", {"sku": "SKU-1", "new_price": 0})
    assert ei.value.code == "BUSINESS_RULE_VIOLATION"


def test_validation_allows_unknown_sku_empty_row():
    """未知 SKU 的空库存行是 mock 契约：缺字段放行、类型错必拦。"""
    v = ResultValidator()
    v.validate("query_inventory", {"channel": "taobao", "sku": "SKU-999"})
    with pytest.raises(ValidationError):
        v.validate("query_inventory", {"channel": "taobao", "sku": "SKU-1", "stock": "很多"})
