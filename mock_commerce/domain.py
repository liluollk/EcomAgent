"""Mock 平台业务逻辑 — 幂等处理、渠道校验、成功响应体构造。"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import HTTPException

from mock_commerce.store import VALID_CHANNELS

_idempotency: dict[str, dict] = {}


def check_channel(channel: str) -> None:
    from sources.channel_registry import DEFAULT_CHANNEL_REGISTRY

    valid = set(VALID_CHANNELS) | DEFAULT_CHANNEL_REGISTRY.enabled_names()
    if channel not in valid:
        raise HTTPException(
            status_code=404,
            detail={"code": 10002, "message": f"渠道不存在: {channel}"},
        )


def ok_response(data: dict | list | str, idempotent_replay: bool = False) -> dict:
    if idempotent_replay and isinstance(data, dict):
        data = {**data, "idempotent_replay": True}
    return {"code": 0, "message": "ok", "data": data}


def idempotent_call(
    data_factory: Callable[[], dict],
    x_idempotency_key: Optional[str],
    record: Optional[Callable[[dict], None]] = None,
) -> dict:
    """幂等写语义：同键重复请求回放首次结果，真实副作用只落一次。

    Args:
        data_factory: 首次请求时构造响应 data（副作用随构造发生）。
        x_idempotency_key: 幂等键（缺失时退化为普通一次性写）。
        record: 首次副作用落库回调（写审计日志）。
    """
    if not x_idempotency_key:
        result = data_factory()
        if record:
            record(result)
        return ok_response(result)
    if x_idempotency_key in _idempotency:
        return ok_response(_idempotency[x_idempotency_key], idempotent_replay=True)
    result = data_factory()
    if record:
        record(result)
    _idempotency[x_idempotency_key] = result
    return ok_response(result, idempotent_replay=False)


def idempotent_post(data_factory, x_idempotency_key: Optional[str]) -> dict:
    """兼容别名（早期仅 POST 有幂等语义）。"""
    return idempotent_call(data_factory, x_idempotency_key)


def reset_idempotency() -> None:
    """清空幂等存储（测试/场景隔离用）。"""
    _idempotency.clear()
