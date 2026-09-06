"""
故障注入 — 确定性故障脚本模拟。

两种控制方式（优先级：脚本 > 环境变量 > 正常）：
  1. 确定性脚本（进程内）：load_script("rate_limit_once_then_success") 装载
     故障步骤队列，apply_fault_pre() 每次调用消费一步，耗尽后恢复正常。
     同输入必同轨迹，适合 Evaluation Harness 确定性验证异常路径。
  2. 环境变量 FAULT_SCENARIO（独立进程演练）：静态故障全程生效。

阶段语义（复现「服务端已生效但客户端超时」的真实危险场景）：
  apply_fault_pre()   副作用发生前调用：可抛 429/409/500、可判 MALFORMED
  apply_fault_post()  副作用落库后调用：仅执行 timeout 的挂起睡眠
"""

from __future__ import annotations

import asyncio
import os
from enum import Enum

from fastapi import HTTPException


class FaultScenario(Enum):
    NORMAL = "normal"
    DELAYED = "delayed"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"
    BUSINESS_CONFLICT = "business_conflict"
    DUPLICATED = "duplicated"
    MALFORMED = "malformed"  # HTTP 200 + code=0，但 data 畸形（缺字段/类型错）


class FaultOutcome(Enum):
    """apply_fault_pre 的结论：正常放行 / 返回畸形成功响应。"""

    PROCEED = "proceed"
    MALFORMED = "malformed"


# 确定性脚本：步骤依次消费，耗尽后恢复正常响应
_FAULT_SCRIPTS: dict[str, list[FaultScenario]] = {
    "rate_limit_once_then_success": [FaultScenario.RATE_LIMITED],
    "timeout_once_then_success": [FaultScenario.TIMEOUT],
    "internal_error_once_then_success": [FaultScenario.INTERNAL_ERROR],
    "malformed_once_then_success": [FaultScenario.MALFORMED],
    "permanent_500": [FaultScenario.INTERNAL_ERROR] * 100,
}

_script_steps: list[FaultScenario] = []
_script_pos = 0
_script_timeout_seconds = 60.0
_pending_timeout = False


def load_script(name: str, *, timeout_seconds: float | None = None) -> None:
    """装载确定性故障脚本（重置进度从头消费）。

    Args:
        name: 脚本名（见 _FAULT_SCRIPTS）。
        timeout_seconds: 脚本内 TIMEOUT 步的睡眠时长（harness 用短睡眠配合
            策略层短超时；独立进程演练缺省 60s）。
    """
    global _script_steps, _script_pos, _script_timeout_seconds, _pending_timeout
    if name not in _FAULT_SCRIPTS:
        raise ValueError(f"未知故障脚本: {name}（可选: {sorted(_FAULT_SCRIPTS)}）")
    _script_steps = list(_FAULT_SCRIPTS[name])
    _script_pos = 0
    _script_timeout_seconds = float(timeout_seconds) if timeout_seconds is not None else 60.0
    _pending_timeout = False


def reset_fault() -> None:
    """清除脚本与挂起状态，恢复正常响应。"""
    global _script_steps, _script_pos, _pending_timeout
    _script_steps = []
    _script_pos = 0
    _pending_timeout = False


def _next_fault() -> FaultScenario | None:
    """取下一个生效故障：脚本步 > 环境变量静态故障 > None。"""
    global _script_pos
    if _script_steps:
        if _script_pos >= len(_script_steps):
            return None
        step = _script_steps[_script_pos]
        _script_pos += 1
        return step
    name = os.environ.get("FAULT_SCENARIO", "normal")
    try:
        fault = FaultScenario(name)
    except ValueError:
        return None
    return None if fault is FaultScenario.NORMAL else fault


def _raise_for(fault: FaultScenario) -> None:
    """把故障映射为第三方风格的 HTTP 错误响应。"""
    if fault is FaultScenario.RATE_LIMITED:
        raise HTTPException(
            status_code=429,
            detail={"code": 10005, "message": "平台限流，请稍后重试"},
        )
    if fault is FaultScenario.INTERNAL_ERROR:
        raise HTTPException(
            status_code=500,
            detail={"code": 99999, "message": "平台内部错误"},
        )
    if fault is FaultScenario.BUSINESS_CONFLICT:
        raise HTTPException(
            status_code=409,
            detail={"code": 10004, "message": "业务状态不允许"},
        )


async def apply_fault_pre() -> FaultOutcome:
    """请求前置阶段：副作用发生前生效（限流/内部错误/业务冲突/畸形）。"""
    global _pending_timeout
    _pending_timeout = False
    fault = _next_fault()
    if fault is None or fault in (FaultScenario.NORMAL, FaultScenario.DUPLICATED):
        return FaultOutcome.PROCEED
    if fault is FaultScenario.MALFORMED:
        return FaultOutcome.MALFORMED
    if fault is FaultScenario.DELAYED:
        await asyncio.sleep(2.0)
        return FaultOutcome.PROCEED
    if fault is FaultScenario.TIMEOUT:
        _pending_timeout = True  # 延后到 post：副作用先落库，再让客户端等超时
        return FaultOutcome.PROCEED
    _raise_for(fault)
    return FaultOutcome.PROCEED


async def apply_fault_post() -> None:
    """请求后置阶段：副作用已落库后执行 timeout 挂起睡眠。"""
    global _pending_timeout
    if not _pending_timeout:
        return
    _pending_timeout = False
    await asyncio.sleep(_script_timeout_seconds)


# 兼容旧调用点：无阶段语义的单入口（等价于 pre，无 post 睡眠）
async def apply_fault(scenario: str | None = None) -> None:
    """旧版单阶段入口：指定 scenario 时按静态故障执行（供简单演练用）。"""
    if scenario is not None:
        try:
            fault = FaultScenario(scenario)
        except ValueError:
            return
        if fault is FaultScenario.DELAYED:
            await asyncio.sleep(2.0)
        elif fault is FaultScenario.TIMEOUT:
            await asyncio.sleep(60.0)
        else:
            _raise_for(fault)
        return
    await apply_fault_pre()
    await apply_fault_post()
