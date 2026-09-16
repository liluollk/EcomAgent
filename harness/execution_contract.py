"""执行契约断言 — 从操作审计与平台副作用日志验证「真的这样执行了」。

分层（与 harness/assertions.py 的决策契约互补）：

  决策契约（assertions.py）：模型是否选对 Tool、是否传对平台 / 商品 / SKU / 目标价。
  执行契约（本模块）      ：权限与审批、状态流转、平台调用次数、幂等键复用、
                            最终回查、真实副作用条数。

执行契约不依赖模型输出文本，而是读三个可印证的事实源：
  1. session/operation_store.py 的操作 JSONL（按 session 隔离，可 rebuild）
  2. mock_commerce.store 的写副作用日志（writes_of）
  3. 本步的事件流里的 permission_request（审批上下文是否与操作同源）

判定口径：
  - 每步只应新增一个操作（除了被前置门拦截的步，那时不产生任何操作记录）；
  - 「副作用条数」按平台写入日志计量，重试与恢复都不得新增第二条；
  - 三态沿用 runner 语义：期望工具未被触发由决策契约抛 NotExercisedError，
    本模块只对「已触发的执行」做断言。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

# 调价写副作用的日志键（mock_commerce.store.record_write 的 op 名）
PRICE_WRITE_OPS = ("taobao_price_update", "douyin_price_update")

_APPROVAL_EVENT_TYPES = ("operation_created", "state_changed", "approval_decided",
                         "platform_attempt", "verification_observed")


@dataclass(frozen=True)
class StoreSnapshot:
    """一步执行前的可观测基线，用于计算「本步增量」。"""

    operations: tuple[str, ...]
    writes: int


def _store_dir() -> str:
    return os.environ.get("AGENT_STORAGE_DIR") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "sessions")
    )


def _operation_store():
    from session.operation_store import OperationStore

    return OperationStore(_store_dir())


def session_operations(session_id: str) -> list[str]:
    """该 session 下已出现过的全部 operation_id（首次出现顺序）。"""
    return _operation_store().operations_for_session(session_id)


def write_count() -> int:
    """调价写副作用总条数（两个平台合计）。"""
    from mock_commerce.store import writes_of

    return sum(len(writes_of(op)) for op in PRICE_WRITE_OPS)


def platform_write_count(platform: str) -> int:
    """指定平台的调价写副作用条数。"""
    from mock_commerce.store import writes_of

    return len(writes_of(f"{platform}_price_update"))


def snapshot_store(session_id: str) -> StoreSnapshot:
    """记录当前基线（在一步对话之前调用）。"""
    return StoreSnapshot(operations=tuple(session_operations(session_id)), writes=write_count())


def operation_records(session_id: str, operation_id: str) -> list[dict]:
    """某个操作的全部审计记录（按写入顺序），便于断言轨迹完整性。"""
    return [r for r in _operation_store().load(session_id) if r.get("operation_id") == operation_id]


def _fail(scenario: str, step: dict, detail: str) -> None:
    raise AssertionError(f"[{scenario}] step「{step['message']}」执行契约不满足：{detail}")


def assert_execution_contract(
    scenario_name: str,
    step: dict,
    events: list,
    *,
    session_id: str,
    before: StoreSnapshot,
) -> None:
    """按 step.expect 声明校验执行契约（未声明 expect 的步自动跳过）。"""
    expect: Optional[dict[str, Any]] = step.get("expect")
    if not expect:
        return

    new_ops = [o for o in session_operations(session_id) if o not in before.operations]
    writes_delta = write_count() - before.writes

    # 1) 前置门拦截步：不应产生任何操作记录，也不应有平台副作用
    if expect.get("operation_created") is False:
        if new_ops:
            _fail(scenario_name, step, f"期望前置门拦截（不建立操作），实际新建操作 {new_ops}")
        if writes_delta != 0:
            _fail(scenario_name, step, f"期望零平台副作用，实际新增 {writes_delta} 条写记录")
        return

    if len(new_ops) != 1:
        _fail(scenario_name, step, f"期望本步新增 1 个调价操作，实际 {len(new_ops)} 个: {new_ops}")
    op_id = new_ops[0]

    records = operation_records(session_id, op_id)
    if not records:
        _fail(scenario_name, step, f"操作 {op_id} 无审计记录（期望已落盘 operation_created）")

    operation = _operation_store().rebuild_operation(session_id, op_id)
    if operation is None:
        _fail(scenario_name, step, f"操作 {op_id} 无法从审计重建")

    # 2) 终态与状态轨迹
    want_state = expect.get("state")
    if want_state and operation.state.value != want_state:
        _fail(
            scenario_name, step,
            f"操作终态 {operation.state.value}，期望 {want_state}（轨迹 {operation.trail}）",
        )
    trail = list(operation.trail)
    for state in expect.get("trail_contains", []):
        if state not in trail:
            _fail(scenario_name, step, f"状态轨迹缺 {state}：实际 {trail}")

    # 3) 平台调用次数与结果分类（幂等键复用：同一操作的键必须唯一）
    attempts = [r for r in records if r.get("type") == "platform_attempt"]
    want_attempts = expect.get("platform_attempts")
    if want_attempts is not None and len(attempts) != want_attempts:
        _fail(scenario_name, step, f"平台调用 {len(attempts)} 次，期望 {want_attempts} 次（{attempts}）")
    want_outcomes = expect.get("attempt_outcomes")
    if want_outcomes is not None:
        outcomes = [r.get("outcome") for r in attempts]
        if outcomes != want_outcomes:
            _fail(scenario_name, step, f"平台调用结果序列 {outcomes}，期望 {want_outcomes}")
    if expect.get("single_idempotency_key"):
        keys = {r.get("idempotency_key") for r in attempts}
        if len(keys) != 1:
            _fail(scenario_name, step, f"重试/恢复必须复用同一幂等键，实际出现 {sorted(keys)}")

    # 4) 审批决定必须与操作同源、且恰好一条
    decisions = [r for r in records if r.get("type") == "approval_decided"]
    want_decisions = expect.get("approval_decisions")
    if want_decisions is not None and len(decisions) != want_decisions:
        _fail(scenario_name, step, f"审批记录 {len(decisions)} 条，期望 {want_decisions} 条")

    # 5) 最终回查结论
    if "verified" in expect:
        observed = [r for r in records if r.get("type") == "verification_observed"]
        if not observed:
            _fail(scenario_name, step, "期望有回查记录（verification_observed），实际没有")
        actual = bool(observed[-1].get("consistent"))
        if actual != bool(expect["verified"]):
            _fail(
                scenario_name, step,
                f"回查结论 consistent={actual}，期望 {expect['verified']}"
                f"（期望价 {observed[-1].get('expected_price')}，实际价 {observed[-1].get('observed_price')}）",
            )

    # 6) 真实副作用条数（写入一次 = 一条；重试与恢复都不得新增）
    if "writes_delta" in expect and writes_delta != expect["writes_delta"]:
        _fail(scenario_name, step, f"本步平台副作用 {writes_delta} 条，期望 {expect['writes_delta']} 条")

    # 7) 审计记录类型覆盖（轨迹完整性）
    present = {r.get("type") for r in records}
    missing = [t for t in expect.get("record_types", []) if t not in present]
    if missing:
        _fail(scenario_name, step, f"审计记录缺 {missing}：实际 {sorted(present)}")

    # 8) 审批事件与操作同源：permission_request 携带的 operation_id 必须是本操作的
    if expect.get("permission_operation_id_matches"):
        request_ids = [
            getattr(e, "operation_id", "") for e in events if getattr(e, "type", "") == "permission_request"
        ]
        if not request_ids:
            _fail(scenario_name, step, "期望发起审批（permission_request），实际没有权限请求事件")
        if any(rid != op_id for rid in request_ids):
            _fail(
                scenario_name, step,
                f"权限事件 operation_id {request_ids} 与协调器操作 {op_id} 不一致（审批上下文未贯通）",
            )
