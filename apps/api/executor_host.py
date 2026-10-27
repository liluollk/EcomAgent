"""API 进程内的 Run 执行宿主(ADR-003)。

首版执行模型把 Run 推进放进 API 进程,不再有独立 Worker 与 Job 队列:

- 进程启动时获取 PostgreSQL advisory lock(单写者断言)并执行恢复扫描;
- ``submit(run_id)`` 由路由在事务提交后调用,非阻塞唤醒后台推进;
- 数据库里的 QUEUED Run 就是队列,``submit`` 丢失不会丢任务;
- 进程退出时停止后台循环并等待当前推进到安全点。

**执行器常驻,不按入口决定装不装。** 两个入口都构造宿主,差别只在注入哪个
driver:生产入口用 :class:`apps.model_driver.WorkspaceModelDriver`(按该
Workspace 的模型配置解析 Backend),演示入口用脚本 driver。能不能推进由
"这个 Workspace 配没配模型"决定,属于数据,不是部署开关——没配就是一次
明确的 :class:`apps.model_driver.WorkspaceNotReady`,而不是静默停在 QUEUED。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from apps.model_driver import WorkspaceModelDriver
from runtime.execution.executor import RunDriver, RunExecutor
from runtime.execution.single_writer import SingleWriterLock


DriverFactory = Callable[[AsyncEngine], RunDriver]


def default_driver_factory(engine: AsyncEngine) -> RunDriver:
    """生产默认 driver:按 Workspace 的模型配置解析 Backend。"""
    return WorkspaceModelDriver(engine)


class ExecutorHost:
    """持有进程级 ``RunExecutor`` 与域 driver 的生命周期。"""

    def __init__(self, engine: AsyncEngine, driver: RunDriver) -> None:
        self._engine = engine
        self.driver = driver
        self.executor = RunExecutor(
            session_factory=lambda: AsyncSession(engine, expire_on_commit=False),
            driver=driver,
            instance_id="api-executor",
            lock=SingleWriterLock(engine),
        )

    async def start(self) -> None:
        await self.executor.start()

    async def aclose(self) -> None:
        await self.executor.aclose()
        aclose = getattr(self.driver, "aclose", None)
        if aclose is not None:
            await aclose()


def build_executor_host(
    engine: AsyncEngine, driver_factory: DriverFactory | None = None
) -> ExecutorHost:
    """构造宿主;未指定工厂时用生产默认 driver。"""
    factory = default_driver_factory if driver_factory is None else driver_factory
    return ExecutorHost(engine, factory(engine))


def notify_run(app: Any, run_id: Any) -> None:
    """唤醒执行器处理某个 Run;无执行宿主时为空操作。"""
    executor = getattr(app.state, "executor", None)
    if isinstance(executor, RunExecutor):
        executor.submit(run_id)
