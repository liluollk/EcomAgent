"""
RetryPolicy — 基于错误分类的指数退避重试策略。

不重试: CLIENT_ERROR, BUSINESS_ERROR
可重试: TRANSIENT_ERROR (429, 503, 连接超时)
可配置: max_retries, base_delay, max_delay, backoff_multiplier
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from execution.error_classification import ErrorCategory, is_retryable

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 10.0
    backoff_multiplier: float = 2.0


@dataclass
class RetryPolicy:
    config: RetryConfig = field(default_factory=RetryConfig)

    def should_retry(self, category: ErrorCategory, attempt: int) -> bool:
        if attempt >= self.config.max_retries:
            return False
        return is_retryable(category)

    def delay_for(self, attempt: int) -> float:
        delay = self.config.base_delay * (self.config.backoff_multiplier ** attempt)
        return min(delay, self.config.max_delay)

    async def execute(
        self,
        fn: Callable[..., Awaitable[Any]],
        classify_error: Callable[[Exception], ErrorCategory],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        last_exception: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                last_exception = exc
                category = classify_error(exc)
                if not self.should_retry(category, attempt):
                    raise
                delay = self.delay_for(attempt)
                logger.warning(
                    "retry attempt %d/%d after %.1fs (category=%s)",
                    attempt + 1,
                    self.config.max_retries,
                    delay,
                    category.value,
                )
                await asyncio.sleep(delay)
        assert last_exception is not None
        raise last_exception