"""Execution Policy 层 — retry / timeout / idempotency / validation。

编排器 ExecutionPolicy 把四个策略串成单次外部调用的执行语义：

    Tool Call（已过 PreToolUse 许可）
      ↓ 生成/传播幂等键（仅写操作）
      ↓ 超时包住每次尝试（读/写分级）
      ↓ 异常 → 错误分类（CLIENT/BUSINESS/TRANSIENT/FATAL）
      ↓ 仅 TRANSIENT 按指数退避重试（同一幂等键）
      ↓ 成功响应过 Schema + Business 校验（HTTP 200 ≠ Tool 成功）
      ↓ ExecutionOutcome（attempts / duration_ms / replay 元数据）

位于 Permission 之后、Adapter 之前，是工具调用进入外部系统前的最后一道闸。
"""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from execution.error_classification import ErrorCategory, classify_exception
from execution.policy.idempotency import IdempotencyPolicy
from execution.policy.retry import RetryPolicy
from execution.policy.timeout import TimeoutPolicy
from execution.policy.validation import ResultValidator

__all__ = [
    "ExecutionOutcome",
    "ExecutionPolicy",
    "DEFAULT_EXECUTION_POLICY",
    "configure_default_policy",
    "set_call_context",
    "get_last_outcome",
]


@dataclass
class ExecutionOutcome:
    """一次受策略管控的工具执行的完整元数据（Trace / 事件回填用）。"""

    value: Any
    attempts: int = 1
    duration_ms: int = 0
    idempotency_key: Optional[str] = None
    idempotent_replay: bool = False


# 每任务调用上下文（session/turn）：contextvars 随任务创建复制，
# 并发工具任务互不串号；幂等键由上下文 + 工具名 + 参数哈希构成。
_call_ctx: ContextVar[dict] = ContextVar("execution_call_ctx", default={})
_last_outcome: ContextVar[Optional[ExecutionOutcome]] = ContextVar(
    "execution_last_outcome", default=None
)


def set_call_context(session_id: str, turn: int) -> None:
    """设置当前任务的调用上下文（幂等键成分）。"""
    _call_ctx.set({"session_id": session_id, "turn": turn})


def get_last_outcome() -> Optional[ExecutionOutcome]:
    """读取本任务最近一次 ExecutionPolicy 执行的元数据（无则 None）。"""
    return _last_outcome.get()


class ExecutionPolicy:
    """单次外部调用的执行策略编排器。

    execute() 返回 ExecutionOutcome；不可重试的最终异常原样上抛，
    由调用方（工具层）归一为可解释文本。
    """

    def __init__(
        self,
        retry: Optional[RetryPolicy] = None,
        timeout: Optional[TimeoutPolicy] = None,
        idempotency: Optional[IdempotencyPolicy] = None,
        validator: Optional[ResultValidator] = None,
    ) -> None:
        self.retry = retry or RetryPolicy()
        self.timeout = timeout or TimeoutPolicy()
        self.idempotency = idempotency or IdempotencyPolicy()
        self.validator = validator or ResultValidator()

    def classify_error(self, exc: BaseException) -> ErrorCategory:
        """异常 → 错误分类（默认鸭子类型识别，可覆写）。"""
        return classify_exception(exc)

    async def execute(
        self,
        operation: str,
        run: Callable[[Optional[str]], Awaitable[Any]],
        *,
        params: Optional[dict] = None,
        validate: bool = True,
    ) -> ExecutionOutcome:
        """按策略执行一次外部调用。

        Args:
            operation: 语义操作名（决定超时分级 / 幂等键 / 校验 Schema）。
            run: 异步执行体，签名为 (idempotency_key) -> value，
                 重试时收到同一个幂等键。
            params: 业务参数（幂等键成分）。
            validate: 成功响应是否过 Schema/Business 校验。

        Returns:
            ExecutionOutcome: 结果值 + attempts/duration_ms/幂等元数据。
        """
        key: Optional[str] = None
        if self.idempotency.should_attach(operation):
            ctx = _call_ctx.get() or {}
            key = self.idempotency.generate_key(
                str(ctx.get("session_id", "unknown")),
                int(ctx.get("turn", 0)),
                operation,
                params or {},
            )

        start = time.perf_counter()
        attempts = 0
        _last_outcome.set(None)  # 清除同任务上一次调用的残留元数据
        while True:
            attempts += 1
            try:
                value = await self.timeout.execute(operation, run, key)
            except Exception as exc:
                category = self.classify_error(exc)
                if not self.retry.should_retry(category, attempts - 1):
                    raise
                await asyncio.sleep(self.retry.delay_for(attempts - 1))
                continue

            if validate and isinstance(value, dict):
                # HTTP 200 ≠ Tool 成功：先 Schema 后 Business，失败按
                # UPSTREAM_INVALID_RESPONSE 上抛（不重试，上游数据有问题）
                self.validator.validate(operation, value)
                self.validator.validate_business(operation, value)

            outcome = ExecutionOutcome(
                value=value,
                attempts=attempts,
                duration_ms=int((time.perf_counter() - start) * 1000),
                idempotency_key=key,
                idempotent_replay=bool(isinstance(value, dict) and value.get("idempotent_replay")),
            )
            _last_outcome.set(outcome)
            return outcome


# 进程级默认策略（工具层经 current_default_policy() 取用；
# harness/测试用 configure_default_policy 换成快退避 + 短超时配置）
DEFAULT_EXECUTION_POLICY = ExecutionPolicy()


def configure_default_policy(policy: ExecutionPolicy) -> None:
    """替换进程级默认策略（仅评测/测试装配用，生产代码勿调）。"""
    global DEFAULT_EXECUTION_POLICY
    DEFAULT_EXECUTION_POLICY = policy
