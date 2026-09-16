"""可恢复的调价操作状态机 — 领域协调器。

把一次调价意图（PriceChangeCommand）从预检走到平台写入、结果回查，
全程由一张显式状态机驱动，并把每一次状态转移、审批决定、平台尝试和回查
观察落到 OperationStore（按 session 隔离的 JSONL）做审计与恢复。

状态集合（PriceChangeState）：
    CREATED          操作已建立，尚未做任何预检
    PRECHECKED       能力 / 快照 / 约束 / 规则校验全部通过
    WAITING_APPROVAL 已发出审批请求，挂起等待用户决定
    EXECUTING        审批通过，正在执行平台写入
    VERIFYING        写入完成（或未知结果），正在回查价格是否生效
    SUCCEEDED        回查一致，调价闭环完成（终态）
    BLOCKED          预检 / 规则 / 业务错误，被拦截（终态）
    REJECTED         审批拒绝，或回查不一致（终态，无平台副作用）
    RETRYING         写入遭遇瞬态错误，准备同键重试
    UNKNOWN_OUTCOME  写入超时且 side_effect_possible，结果未知 → 必须回查

非法状态转移一律抛 IllegalStateTransition，由调用方决定如何处理。

幂等键：由 operation_id 派生（price_{operation_id}），整个生命周期固定，
重试与恢复都复用同一个键——绝不会因为重试/恢复而换键或盲目二次写入。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from integrations.commerce.capabilities import get_capabilities, require_capability
from integrations.commerce.cost_provider import get_cost_provider
from integrations.commerce.price_models import (
    PriceChangeCommand,
    PriceConstraints,
    PriceError,
    PriceErrorCode,
    PricePlatform,
    PriceVerification,
    ProductRef,
    ProductSnapshot,
)
from permission.pre_tool_use import PreToolUseAction
from permission.rule_engine import price_above_cost_rule

from events.agent_event import PermissionRequestEvent


class PriceChangeState(str, Enum):
    """调价操作状态（str 子类便于 JSON 序列化与持久化）。"""

    CREATED = "CREATED"
    PRECHECKED = "PRECHECKED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"
    RETRYING = "RETRYING"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"

    @property
    def terminal(self) -> bool:
        """终态：不再接受任何转移。"""
        return self in (PriceChangeState.SUCCEEDED, PriceChangeState.REJECTED, PriceChangeState.BLOCKED)


# 允许的状态转移表——不在表内的转移即「非法」，必须被拒绝。
_ALLOWED_TRANSITIONS: dict[PriceChangeState, frozenset[PriceChangeState]] = {
    PriceChangeState.CREATED: frozenset({PriceChangeState.PRECHECKED, PriceChangeState.BLOCKED}),
    PriceChangeState.PRECHECKED: frozenset({PriceChangeState.WAITING_APPROVAL, PriceChangeState.BLOCKED}),
    PriceChangeState.WAITING_APPROVAL: frozenset({
        PriceChangeState.EXECUTING, PriceChangeState.REJECTED, PriceChangeState.BLOCKED,
    }),
    PriceChangeState.EXECUTING: frozenset({
        PriceChangeState.VERIFYING, PriceChangeState.RETRYING,
        PriceChangeState.UNKNOWN_OUTCOME, PriceChangeState.BLOCKED,
    }),
    PriceChangeState.RETRYING: frozenset({PriceChangeState.EXECUTING, PriceChangeState.BLOCKED}),
    PriceChangeState.UNKNOWN_OUTCOME: frozenset({PriceChangeState.VERIFYING, PriceChangeState.BLOCKED}),
    PriceChangeState.VERIFYING: frozenset({
        PriceChangeState.SUCCEEDED, PriceChangeState.REJECTED,
        PriceChangeState.RETRYING, PriceChangeState.BLOCKED,
    }),
    PriceChangeState.SUCCEEDED: frozenset(),
    PriceChangeState.REJECTED: frozenset(),
    PriceChangeState.BLOCKED: frozenset(),
}


class IllegalStateTransition(Exception):
    """非法状态转移——状态机拒绝接受该转移时抛出。"""

    def __init__(
        self,
        current: PriceChangeState,
        target: PriceChangeState,
        operation_id: str = "",
    ) -> None:
        super().__init__(f"非法状态转移 {current.value} -> {target.value} (operation={operation_id or '?'})")
        self.current = current
        self.target = target
        self.operation_id = operation_id


def assert_transition(current: PriceChangeState, target: PriceChangeState, operation_id: str = "") -> None:
    """声明一次状态转移合法；非法则抛 IllegalStateTransition。"""
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise IllegalStateTransition(current, target, operation_id)


@dataclass
class PriceChangeOperation:
    """一次调价操作的运行时状态（可变，由协调器推进）。

    属性里 command / idempotency_key / operation_id 一旦建立就固定，
    重试与恢复都复用它们，不得更换。
    """

    operation_id: str
    command: PriceChangeCommand
    state: PriceChangeState = PriceChangeState.CREATED
    idempotency_key: str = ""
    snapshot: Optional[ProductSnapshot] = None
    constraints: Optional[PriceConstraints] = None
    rule_summary: str = ""
    receipt: Optional[Any] = None
    verification: Optional[PriceVerification] = None
    attempts: int = 0
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    trail: list[str] = field(default_factory=list)

    def transition_to(self, target: PriceChangeState, *, reason: str = "") -> None:
        """带合法性检查的就地状态转移，并维护 trail。"""
        assert_transition(self.state, target, self.operation_id)
        self.state = target
        self.trail.append(target.value)
        if reason:
            self.error = reason


# 审批解析器签名：接收权限请求事件，返回用户是否批准。
Approver = Callable[[PermissionRequestEvent], Any]


class PriceChangeCoordinator:
    """调价协调器 — 按固定顺序驱动状态机，并把审计落盘。

    顺序固定（不可被调用方打乱）：
        能力检查 → 查询快照 → 查询内部价格约束 → 规则校验
        → 审批 → 平台写入 → 结果回查

    成本价只来自内部 CostProvider（经 get_cost_provider），绝不由命令/模型传入。
    规则校验复用 permission.rule_engine.price_above_cost_rule 的成本保护语义
    （只读调用，不修改该模块）。
    """

    def __init__(
        self,
        platform: PricePlatform,
        *,
        session_id: str = "",
        operation_store: Any = None,
        cost_provider: Any = None,
        rule_check: Optional[Callable[[str, dict], Any]] = None,
        max_retries: int = 3,
    ) -> None:
        self.platform = platform
        self.session_id = session_id
        self.store = operation_store
        self.cost_provider = cost_provider
        self.rule_check = rule_check or price_above_cost_rule
        self.max_retries = max_retries

    # ------------------------------------------------------------------
    # 幂等键
    # ------------------------------------------------------------------
    def _idempotency_key(self, command: PriceChangeCommand) -> str:
        """由 operation_id 派生的稳定幂等键——生命周期内不变。"""
        return f"price_{command.operation_id}"

    # ------------------------------------------------------------------
    # 审计落盘
    # ------------------------------------------------------------------
    def _emit(self, operation_id: str, event_type: str, **payload: Any) -> None:
        if self.store is None:
            return
        self.store.append(self.session_id, {
            "type": event_type,
            "operation_id": operation_id,
            **payload,
        })

    def _record_state(self, op: PriceChangeOperation, target: PriceChangeState, *, reason: str = "") -> None:
        op.transition_to(target, reason=reason)
        self._emit(op.operation_id, "state_changed", from_state="", to_state=target.value, reason=reason)

    # ------------------------------------------------------------------
    # 阶段 1：建立操作
    # ------------------------------------------------------------------
    async def create(self, command: PriceChangeCommand) -> PriceChangeOperation:
        """建立操作（CREATED），生成固定幂等键并落盘 operation_created。"""
        op = PriceChangeOperation(
            operation_id=command.operation_id,
            command=command,
            state=PriceChangeState.CREATED,
            idempotency_key=self._idempotency_key(command),
        )
        self._emit(op.operation_id, "operation_created", to_state=PriceChangeState.CREATED.value, **_command_view(command))
        return op

    # ------------------------------------------------------------------
    # 阶段 2：预检（能力 / 快照 / 约束 / 规则）
    # ------------------------------------------------------------------
    async def precheck(self, op: PriceChangeOperation) -> None:
        """能力检查 → 查询快照 → 查询内部价格约束 → 规则校验。

        任一步失败进入 BLOCKED；全部通过进入 PRECHECKED。
        """
        ref = op.command.product_ref
        try:
            # 1) 能力检查
            require_capability(ref.platform, "query_snapshot")
            require_capability(ref.platform, "update_price")
            require_capability(ref.platform, "verify_price")
            capabilities = get_capabilities(ref.platform)

            # 2) 查询快照
            snapshot = await self.platform.query_snapshot(ref)
            op.snapshot = snapshot

            # 3) 查询内部价格约束（成本价只来自 CostProvider）
            cost = self._cost_price(ref)
            constraints = PriceConstraints(
                product_ref=ref,
                cost_price=Decimal(str(cost)) if cost is not None else None,
                price_scale=capabilities.price_scale,
                locked_by_activity=bool(snapshot.activity_locked),
                source="cost_provider",
            )
            op.constraints = constraints

            # 4) 规则校验（复用成本保护语义，只读调用）
            result = self.rule_check(
                "update_price",
                {"new_price": op.command.target_price, "cost_price": cost if cost is not None else 0},
            )
            if getattr(result, "action", PreToolUseAction.ALLOW) != PreToolUseAction.ALLOW:
                op.rule_summary = f"成本保护拦截：{getattr(result, 'reason', '')}"
                self._record_state(op, PriceChangeState.BLOCKED, reason=op.rule_summary)
                return

            op.rule_summary = (
                f"成本保护通过：目标价 {op.command.target_price} "
                f">= 成本价 {cost if cost is not None else '未知(放行)'}"
            )
        except PriceError as exc:
            self._record_state(op, PriceChangeState.BLOCKED, reason=str(exc))
            return
        except Exception as exc:  # 预检阶段任何意外都归为拦截
            self._record_state(op, PriceChangeState.BLOCKED, reason=f"预检异常：{exc}")
            return

        self._record_state(op, PriceChangeState.PRECHECKED)

    def _cost_price(self, ref: ProductRef) -> Optional[float]:
        provider = self.cost_provider or get_cost_provider()
        try:
            return provider.get_cost_price(ref.platform, ref.sku_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 阶段 3：审批
    # ------------------------------------------------------------------
    def _build_approval_event(self, op: PriceChangeOperation) -> PermissionRequestEvent:
        ref = op.command.product_ref
        return PermissionRequestEvent(
            request_id=f"op_{op.operation_id}",
            tool_name="update_price",
            tool_input={"operation_id": op.operation_id, "target_price": str(op.command.target_price)},
            reason="调价将产生平台副作用，需要用户确认",
            operation_id=op.operation_id,
            platform=ref.platform,
            product_ref={
                "platform": ref.platform,
                "shop_id": ref.shop_id,
                "product_id": ref.product_id,
                "sku_id": ref.sku_id,
            },
            target_price=str(op.command.target_price),
            rule_summary=op.rule_summary,
        )

    def begin_approval(self, op: PriceChangeOperation) -> Optional[PermissionRequestEvent]:
        """PRECHECKED → WAITING_APPROVAL，返回待决的权限请求事件。"""
        if op.state != PriceChangeState.PRECHECKED:
            return None
        self._record_state(op, PriceChangeState.WAITING_APPROVAL, reason="等待审批")
        return self._build_approval_event(op)

    async def decide_approval(self, op: PriceChangeOperation, approved: bool) -> None:
        """WAITING_APPROVAL → EXECUTING（批准）或 REJECTED（拒绝）。"""
        if op.state != PriceChangeState.WAITING_APPROVAL:
            return
        self._emit(op.operation_id, "approval_decided", decision="approved" if approved else "rejected",
                   rule_summary=op.rule_summary)
        if approved:
            self._record_state(op, PriceChangeState.EXECUTING, reason="审批通过")
        else:
            self._record_state(op, PriceChangeState.REJECTED, reason="审批拒绝（无平台副作用）")

    async def request_approval(self, op: PriceChangeOperation, approver: Approver) -> bool:
        """发起审批并等待决定；返回是否批准。"""
        event = self.begin_approval(op)
        if event is None:
            return op.state == PriceChangeState.EXECUTING
        decision = approver(event)
        if asyncio.iscoroutine(decision):
            decision = await decision
        await self.decide_approval(op, bool(decision))
        return bool(decision)

    # ------------------------------------------------------------------
    # 阶段 4：平台写入（含重试 / 未知结果）
    # ------------------------------------------------------------------
    async def _write_until_settled(self, op: PriceChangeOperation) -> None:
        """执行平台写入并就地处理重试 / 未知结果 / 错误。

        结束时状态为 VERIFYING（写入成功或未知结果已转回查）、
        RETRYING（瞬态未耗尽）、或 BLOCKED（不可恢复错误）。
        """
        while True:
            if op.state not in (PriceChangeState.EXECUTING, PriceChangeState.RETRYING):
                break
            # RETRYING 回到 EXECUTING 准备同键重试
            if op.state == PriceChangeState.RETRYING:
                self._record_state(op, PriceChangeState.EXECUTING, reason="同键重试")
            try:
                receipt = await self.platform.apply_price(
                    op.command, idempotency_key=op.idempotency_key,
                )
                op.receipt = receipt
                op.attempts += 1
                self._emit(op.operation_id, "platform_attempt",
                           outcome="success", attempt=op.attempts,
                           idempotency_key=op.idempotency_key,
                           idempotent_replay=bool(getattr(receipt, "idempotent_replay", False)))
                self._record_state(op, PriceChangeState.VERIFYING, reason="写入完成")
                return
            except PriceError as exc:
                op.attempts += 1
                if exc.side_effect_possible:
                    # 写入超时且可能已落库：进入未知结果，必须回查而非重试写
                    self._emit(op.operation_id, "platform_attempt",
                               outcome="unknown", attempt=op.attempts,
                               idempotency_key=op.idempotency_key,
                               error=str(exc))
                    self._record_state(op, PriceChangeState.UNKNOWN_OUTCOME,
                                       reason="写入超时，结果未知，转回查")
                    # 未知结果 → 直接回查（不再写）
                    self._record_state(op, PriceChangeState.VERIFYING, reason="未知结果回查")
                    return
                if exc.code.retryable and op.attempts < self.max_retries:
                    self._emit(op.operation_id, "platform_attempt",
                               outcome="transient", attempt=op.attempts,
                               idempotency_key=op.idempotency_key,
                               error=str(exc))
                    self._record_state(op, PriceChangeState.RETRYING, reason=str(exc))
                    await asyncio.sleep(0)  # 让出，避免测试中自旋过紧
                    continue
                # 业务 / 客户端 / 致命错误：不可重试
                self._emit(op.operation_id, "platform_attempt",
                           outcome="error", attempt=op.attempts,
                           idempotency_key=op.idempotency_key,
                           error=str(exc))
                self._record_state(op, PriceChangeState.BLOCKED, reason=str(exc))
                return
            except Exception as exc:  # 非 PriceError：按致命错误处理
                op.attempts += 1
                self._emit(op.operation_id, "platform_attempt",
                           outcome="error", attempt=op.attempts,
                           idempotency_key=op.idempotency_key,
                           error=str(exc))
                self._record_state(op, PriceChangeState.BLOCKED, reason=f"写入异常：{exc}")
                return

    # ------------------------------------------------------------------
    # 阶段 5：结果回查
    # ------------------------------------------------------------------
    async def verify(self, op: PriceChangeOperation) -> None:
        """VERIFYING → 回查价格是否生效。

        一致 → SUCCEEDED；不一致 → REJECTED；回查瞬态错误在限额内 RETRYING。
        """
        if op.state != PriceChangeState.VERIFYING:
            return
        attempts = 0
        while True:
            attempts += 1
            try:
                verification = await self.platform.verify_price(
                    op.command.product_ref, op.command.target_price,
                )
                op.verification = verification
                self._emit(op.operation_id, "verification_observed",
                           consistent=bool(verification.consistent),
                           expected_price=str(verification.expected_price),
                           observed_price=str(verification.observed_price)
                           if verification.observed_price is not None else None,
                           attempts=attempts)
                if verification.consistent:
                    self._record_state(op, PriceChangeState.SUCCEEDED, reason="回查一致")
                    return
                self._record_state(op, PriceChangeState.REJECTED, reason="回查不一致，价格未生效")
                return
            except PriceError as exc:
                if exc.code.retryable and attempts < self.max_retries:
                    self._record_state(op, PriceChangeState.RETRYING, reason=f"回查瞬态：{exc}")
                    continue
                self._record_state(op, PriceChangeState.BLOCKED, reason=f"回查失败：{exc}")
                return
            except Exception as exc:
                self._record_state(op, PriceChangeState.BLOCKED, reason=f"回查异常：{exc}")
                return

    # ------------------------------------------------------------------
    # 主流程：从建立到闭环
    # ------------------------------------------------------------------
    async def execute(self, command: PriceChangeCommand, approver: Approver) -> PriceChangeOperation:
        """完整执行一次调价：建立 → 预检 → 审批 → 写入 → 回查。"""
        op = await self.create(command)
        await self.precheck(op)
        if op.state == PriceChangeState.BLOCKED:
            return op
        await self.request_approval(op, approver)
        if op.state != PriceChangeState.EXECUTING:
            return op  # REJECTED：不触发任何平台副作用
        await self._write_until_settled(op)
        if op.state == PriceChangeState.VERIFYING:
            await self.verify(op)
        return op

    # ------------------------------------------------------------------
    # 恢复姿态（从 JSONL 重建后继续）
    # ------------------------------------------------------------------
    async def recover_unknown(self, op: PriceChangeOperation) -> None:
        """UNKNOWN_OUTCOME 恢复：复用同一 operation_id 与幂等键回查，绝不二次写入。"""
        if op.state != PriceChangeState.UNKNOWN_OUTCOME:
            # 若已转回查阶段（VERIFYING），直接回查；否则拒绝非法恢复
            if op.state == PriceChangeState.VERIFYING:
                await self.verify(op)
                return
            raise IllegalStateTransition(op.state, PriceChangeState.VERIFYING, op.operation_id)
        self._record_state(op, PriceChangeState.VERIFYING, reason="未知结果回查")
        await self.verify(op)

    async def resume(self, op: PriceChangeOperation, approver: Optional[Approver] = None) -> PriceChangeOperation:
        """从持久化状态恢复操作，按当前状态继续未完成的步骤。

        覆盖三种恢复姿态：
          - 审批挂起（WAITING_APPROVAL）：重新发起审批决定
          - 执行中断（EXECUTING / RETRYING）：继续写入
          - 未知结果（UNKNOWN_OUTCOME）：回查恢复（不二次写入）
        """
        if op.state == PriceChangeState.WAITING_APPROVAL:
            if approver is None:
                return op
            decision = approver(self._build_approval_event(op))
            if asyncio.iscoroutine(decision):
                decision = await decision
            await self.decide_approval(op, bool(decision))
        if op.state in (PriceChangeState.EXECUTING, PriceChangeState.RETRYING):
            await self._write_until_settled(op)
        if op.state == PriceChangeState.UNKNOWN_OUTCOME:
            await self.recover_unknown(op)
        if op.state == PriceChangeState.VERIFYING:
            await self.verify(op)
        return op


def _command_view(command: PriceChangeCommand) -> dict:
    """命令的可序列化视图，用于 operation_created 审计记录。"""
    ref = command.product_ref
    return {
        "platform": ref.platform,
        "shop_id": ref.shop_id,
        "product_id": ref.product_id,
        "sku_id": ref.sku_id,
        "target_price": str(command.target_price),
        "requester": command.requester,
        "reason": command.reason,
    }


__all__ = [
    "IllegalStateTransition",
    "PriceChangeCoordinator",
    "PriceChangeOperation",
    "PriceChangeState",
    "assert_transition",
]
