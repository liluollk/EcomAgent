"""Worker 进程配置(计划 04 Task 5)。

frozen pydantic 配置;``from_env`` 从环境变量构造,默认值适合测试(本地
Postgres、短租约、可关闭装配校验)。HTTP 健康端点不在本任务范围,
``health_port`` 作为可选端口预留。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

_SECONDS_ENVS: Final = (
    ("RUNTIME_LEASE_SECONDS", "lease_duration"),
    ("RUNTIME_HEARTBEAT_SECONDS", "heartbeat_interval"),
    ("RUNTIME_POLL_SECONDS", "poll_interval"),
    ("RUNTIME_SHUTDOWN_SECONDS", "shutdown_timeout"),
)


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean: {raw!r}")


class WorkerSettings(BaseModel):
    """Worker 进程配置;``from_env`` 是唯一推荐构造入口。"""

    model_config = ConfigDict(frozen=True)

    database_url: str = "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime"
    worker_id: str = "worker-1"
    lease_duration: timedelta = timedelta(seconds=30)
    heartbeat_interval: timedelta = timedelta(seconds=10)
    poll_interval: timedelta = timedelta(milliseconds=200)
    shutdown_timeout: timedelta = timedelta(seconds=30)
    validate_assembly: bool = True
    health_port: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerSettings:
        """从环境变量构造;未设置的项取默认值,非法值抛 ValueError。"""
        source: Mapping[str, str] = os.environ if env is None else env
        kwargs: dict[str, Any] = {}

        database_url = source.get("RUNTIME_DATABASE_URL")
        if database_url:
            kwargs["database_url"] = database_url
        worker_id = source.get("RUNTIME_WORKER_ID")
        if worker_id:
            kwargs["worker_id"] = worker_id

        for env_name, field_name in _SECONDS_ENVS:
            raw = source.get(env_name)
            if raw:
                try:
                    kwargs[field_name] = timedelta(seconds=float(raw))
                except ValueError as error:
                    raise ValueError(
                        f"{env_name} must be a number of seconds: {raw!r}"
                    ) from error

        validate_raw = source.get("RUNTIME_VALIDATE_ASSEMBLY")
        if validate_raw is not None:
            kwargs["validate_assembly"] = _parse_bool(
                "RUNTIME_VALIDATE_ASSEMBLY", validate_raw
            )

        port_raw = source.get("RUNTIME_HEALTH_PORT")
        if port_raw:
            try:
                kwargs["health_port"] = int(port_raw)
            except ValueError as error:
                raise ValueError(
                    f"RUNTIME_HEALTH_PORT must be an integer: {port_raw!r}"
                ) from error

        return cls(**kwargs)
