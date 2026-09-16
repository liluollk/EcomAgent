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
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import Optional

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
    # —— 双平台调价故障（Task 3/5） ——
    TIMEOUT_PRE = "timeout_pre"  # 副作用落库「前」超时（客户端先放弃，写未必发生）
    CAPABILITY_REFUSED = "capability_refused"  # 平台返回「能力不支持」
    VERIFY_MISMATCH = "verify_mismatch"  # 回查返回的价格与目标价不一致
    CLIENT_ERROR = "client_error"  # 参数/权限类错误（用于覆盖 CLIENT_ERROR 映射）


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
    # —— 双平台调价确定性脚本（Task 3/5） ——
    # 新平台等价脚本：让既有的限流一次成功 / 畸形一次成功在新协议形态下也可用
    "platform_rate_limit_once_then_success": [FaultScenario.RATE_LIMITED],
    "platform_malformed_once_then_success": [FaultScenario.MALFORMED],
    # 写入前超时（副作用落库前客户端已放弃）
    "write_timeout_before_commit": [FaultScenario.TIMEOUT_PRE],
    # 写入后超时（副作用已落库，复现「服务端已生效但客户端超时」）
    "write_timeout_after_commit": [FaultScenario.TIMEOUT],
    # 持续业务错误
    "business_error_permanent": [FaultScenario.BUSINESS_CONFLICT] * 100,
    # 平台返回能力不支持
    "capability_refused": [FaultScenario.CAPABILITY_REFUSED],
    # 回查价格与目标价不一致
    "verify_mismatch": [FaultScenario.VERIFY_MISMATCH],
    # 回查超时（复用 TIMEOUT：副作用落库后挂起，对只读回查即为「回查超时」）
    "verify_timeout": [FaultScenario.TIMEOUT],
}

_script_steps: list[FaultScenario] = []
_script_pos = 0
_script_timeout_seconds = 60.0
_script_methods: frozenset[str] | None = None  # 定向消费：仅匹配的 HTTP 方法消费故障步
_pending_timeout = False
_skip_next_write = False  # TIMEOUT_PRE 期间置位：路由据此跳过副作用落库

# 当前请求的 HTTP 方法（FastAPI 中间件写入，供定向消费判定）
request_method_var: ContextVar[str] = ContextVar("mock_fault_request_method", default="")


def load_script(name: str, *, timeout_seconds: float | None = None,
                methods: set[str] | None = None) -> None:
    """装载确定性故障脚本（重置进度从头消费）。

    Args:
        name: 脚本名（见 _FAULT_SCRIPTS）。
        timeout_seconds: 脚本内 TIMEOUT 步的睡眠时长（harness 用短睡眠配合
            策略层短超时；独立进程演练缺省 60s）。
        methods: 定向消费的 HTTP 方法集合（如 {"PUT"}）；为 None 时不限方法
            （保持旧语义：任意请求消费一步）。
    """
    global _script_steps, _script_pos, _script_timeout_seconds, _script_methods, _pending_timeout
    if name not in _FAULT_SCRIPTS:
        raise ValueError(f"未知故障脚本: {name}（可选: {sorted(_FAULT_SCRIPTS)}）")
    _script_steps = list(_FAULT_SCRIPTS[name])
    _script_pos = 0
    _script_timeout_seconds = float(timeout_seconds) if timeout_seconds is not None else 60.0
    _script_methods = frozenset(methods) if methods else None
    _pending_timeout = False


def reset_fault() -> None:
    """清除脚本与挂起状态，恢复正常响应。"""
    global _script_steps, _script_pos, _script_methods, _pending_timeout, _skip_next_write
    _script_steps = []
    _script_pos = 0
    _script_methods = None
    _pending_timeout = False
    _skip_next_write = False


def consume_skip_write() -> bool:
    """读取并清除「跳过本次写副作用」标记（TIMEOUT_PRE 故障消费后置位）。"""
    global _skip_next_write
    value = _skip_next_write
    _skip_next_write = False
    return value


def _next_fault() -> FaultScenario | None:
    """取下一个生效故障：脚本步 > 环境变量静态故障 > None。

    脚本步定向消费：装载时声明 methods 后，仅匹配方法的请求消费故障步——
    真实模型的只读预检（如 SKILL 建议先 query_inventory）不消耗写故障
    （PUT/POST），避免唯一故障步被预检吃掉、重试契约假阴性。
    """
    global _script_pos
    if _script_steps:
        if _script_methods is not None:
            method = request_method_var.get().upper()
            if method not in _script_methods:
                return None  # 非目标语义请求：不消费，正常放行
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


# ---------------------------------------------------------------------------
# 双平台调价故障接口（Task 3/5）
#
# 老路由仍走 apply_fault_pre/apply_fault_post（HTTP 状态码语义：429/409/500/畸形）。
# 淘宝(TOP 信封) / 抖店(JSON 信封) 路由走下面这套「语义类别」接口：同一个确定性
# 脚本在两种协议形态下都可用，路由负责把语义类别翻译成各自平台的错误码与信封。
# ---------------------------------------------------------------------------

@dataclass
class PlatformFault:
    """一次平台请求的故障结论。

    kind:
      proceed         正常放行（可能带 verify_mismatch 变种，由路由自行判断）
      malformed       信封完好 code=0，但 data 含类型违例字段（校验层拦截）
      timeout_pre     副作用落库前超时（路由应跳过写副作用）
      timeout_post    副作用已落库，post 阶段挂起（复现「客户端超时但服务端已生效」）
      verify_mismatch 回查返回的价格与目标不一致
      error           平台业务错误，code 为平台特定码，走信封错误（HTTP 200）
    http_status: 非 0 表示以该 HTTP 状态码抛出（用于 5xx 系统错误）
    """

    kind: str
    code: int = 0
    msg: str = ""
    http_status: int = 0


# 语义类别 → 平台特定错误码（同一脚本在两平台下映射出不同的平台码）
_TAOBAO_FAULT_CODE = {
    "client": 27,      # 商品不存在 / 参数非法（计划里 15/21/26/27 均归 CLIENT_ERROR）
    "business": 40,    # 价格低于成本 / 合规拦截
    "transient": 7,    # 限流
    "fatal": 500,      # 系统错误
    "capability": 9998,  # 能力不支持（计划未指定，自行固定）
}
_DOUYIN_FAULT_CODE = {
    "client": 40010,
    "business": 30001,
    "transient": 20001,
    "fatal": 50000,
    "capability": 90000,
}
_PLATFORM_FAULT_MSG = {
    "client": "参数或权限错误",
    "business": "业务规则拒绝（低于成本/活动锁价）",
    "transient": "平台限流或系统繁忙",
    "fatal": "平台系统错误",
    "capability": "平台不支持该能力",
}


async def apply_fault_pre_platform(platform: str) -> PlatformFault:
    """平台路由前置阶段：消费一步故障脚本，翻译成平台特定的故障结论。

    与 apply_fault_pre 共用同一个脚本队列，但产出的是语义类别而非 HTTP 状态码，
    便于淘宝/抖店各自映射到自己的错误码与信封。
    """
    global _pending_timeout, _skip_next_write
    _skip_next_write = False
    fault = _next_fault()
    if fault is None or fault in (FaultScenario.NORMAL, FaultScenario.DUPLICATED, FaultScenario.DELAYED):
        return PlatformFault("proceed")
    if fault is FaultScenario.MALFORMED:
        return PlatformFault("malformed")
    if fault is FaultScenario.TIMEOUT:
        _pending_timeout = True  # 延后到 post：副作用先落库，再让客户端等超时
        return PlatformFault("timeout_post")
    if fault is FaultScenario.TIMEOUT_PRE:
        # 副作用落库「前」超时：先睡（客户端在此期间放弃），并标记本次跳过写
        await asyncio.sleep(_script_timeout_seconds)
        _skip_next_write = True
        return PlatformFault("timeout_pre")
    if fault is FaultScenario.VERIFY_MISMATCH:
        return PlatformFault("verify_mismatch")
    category = {
        FaultScenario.RATE_LIMITED: "transient",
        FaultScenario.INTERNAL_ERROR: "fatal",
        FaultScenario.BUSINESS_CONFLICT: "business",
        FaultScenario.CAPABILITY_REFUSED: "capability",
        FaultScenario.CLIENT_ERROR: "client",
    }.get(fault)
    if category is not None:
        code_map = _TAOBAO_FAULT_CODE if platform == "taobao" else _DOUYIN_FAULT_CODE
        return PlatformFault("error", code=code_map[category], msg=_PLATFORM_FAULT_MSG[category])
    # 未识别的脚本步：放行（不破坏旧路由语义）
    return PlatformFault("proceed")


async def apply_fault_post_platform() -> None:
    """平台路由后置阶段：仅执行 timeout_post 的挂起睡眠。"""
    global _pending_timeout
    if not _pending_timeout:
        return
    _pending_timeout = False
    await asyncio.sleep(_script_timeout_seconds)
