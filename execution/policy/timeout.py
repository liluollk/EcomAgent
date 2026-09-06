"""
TimeoutPolicy — 工具执行超时控制。

读写操作区分超时：读操作默认 10s，写操作默认 30s。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class TimeoutConfig:
    default_seconds: float = 30.0
    read_seconds: float = 10.0
    write_seconds: float = 30.0


class TimeoutError(Exception):
    """工具执行超时异常。"""

    def __init__(self, tool_name: str, timeout: float) -> None:
        super().__init__(f"工具 {tool_name} 执行超时 ({timeout}s)")
        self.tool_name = tool_name
        self.timeout = timeout


@dataclass
class TimeoutPolicy:
    config: TimeoutConfig = field(default_factory=TimeoutConfig)

    def timeout_for(self, tool_name: str) -> float:
        read_prefixes = ("query_", "get_", "list_", "search_")
        if any(tool_name.startswith(p) for p in read_prefixes):
            return self.config.read_seconds
        return self.config.write_seconds

    async def execute(
        self,
        tool_name: str,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        timeout = self.timeout_for(tool_name)
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(tool_name, timeout)