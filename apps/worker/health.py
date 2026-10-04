"""Worker 健康状态(计划 04 Task 5)。

Health 分开报告三个独立布尔:

- ``process_alive``:``report()`` 能执行即进程/事件循环存活;
- ``database_reachable``:轻量 ``SELECT 1`` 探测,任何异常视为不可达;
- ``assembly_valid``:启动装配校验结果(由组合根注入的来源读取)。

报告为 frozen 视图,三个布尔互相独立、分别注入,便于故障测试。HTTP 健康
端点不在本任务范围;``report()`` 可被任何外部调用方(端口服务、探针代理)
复用。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncEngine


DatabaseProbe = Callable[[], Awaitable[bool]]
"""数据库可达性探针:返回 True 表示可达;任何失败都应返回 False。"""

AssemblyValidSource = Callable[[], bool]
"""装配校验结果来源:读取启动校验的当前结论。"""


class HealthReport(BaseModel):
    """一次健康检查的 frozen 视图;三个布尔互相独立。"""

    model_config = ConfigDict(frozen=True)

    process_alive: bool
    database_reachable: bool
    assembly_valid: bool


class HealthReporter:
    """聚合三个独立健康布尔;数据库探针与校验来源均注入,便于测试故障。"""

    def __init__(
        self,
        *,
        database_probe: DatabaseProbe,
        assembly_valid_source: AssemblyValidSource,
    ) -> None:
        self._database_probe = database_probe
        self._assembly_valid_source = assembly_valid_source

    async def report(self) -> HealthReport:
        """执行一次健康检查;数据库探测失败(含抛异常)只影响该布尔。"""
        try:
            database_reachable = await self._database_probe()
        except Exception:  # noqa: BLE001 — 探测的任意失败都视为不可达
            database_reachable = False
        return HealthReport(
            process_alive=True,
            database_reachable=database_reachable,
            assembly_valid=bool(self._assembly_valid_source()),
        )


def database_probe_from_engine(engine: AsyncEngine) -> DatabaseProbe:
    """基于引擎的轻量数据库探测:打开连接执行 ``SELECT 1``。"""

    async def probe() -> bool:
        try:
            async with engine.connect() as connection:
                await connection.execute(sa.text("SELECT 1"))
        except Exception:  # noqa: BLE001 — 任何连接/查询失败都视为不可达
            return False
        return True

    return probe
