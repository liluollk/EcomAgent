"""API 进程内的 Run 执行宿主(ADR-003)。

首版执行模型把 Run 推进放进 API 进程,不再有独立 Worker 与 Job 队列:

- 进程启动时获取 PostgreSQL advisory lock(单写者断言)并执行恢复扫描;
- ``submit(run_id)`` 由路由在事务提交后调用,非阻塞唤醒后台推进;
- 数据库里的 QUEUED Run 就是队列,``submit`` 丢失不会丢任务;
- 进程退出时停止后台循环并等待当前推进到安全点。

生产环境尚未接入真实模型与平台 Adapter,因此宿主只在 ``RUNTIME_E2E=1``
(或显式注入 driver)时装配;没有宿主时 API 仍然创建 Run,只是不推进——
这与"执行能力未部署"的语义一致,不会伪装成已执行。
"""

from __future__ import annotations

import os
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from apps.scripted_e2e import ProductDriverRegistry
from runtime.execution.executor import RunExecutor
from runtime.execution.single_writer import SingleWriterLock


_ENABLED_VALUES: Final = frozenset({"1", "true", "yes", "on"})


def e2e_executor_enabled() -> bool:
    """``RUNTIME_E2E=1`` 时装配脚本化执行宿主(与 API 的 E2E 种子端点同开关)。"""
    return os.environ.get("RUNTIME_E2E", "").strip().lower() in _ENABLED_VALUES


class ExecutorHost:
    """持有进程级 ``RunExecutor`` 与商品域 driver 的生命周期。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.driver = ProductDriverRegistry(engine)
        self.executor = RunExecutor(
            session_factory=lambda: AsyncSession(engine, expire_on_commit=False),
            driver=self.driver,
            instance_id="api-executor",
            lock=SingleWriterLock(engine),
        )

    async def start(self) -> None:
        await self.executor.start()

    async def aclose(self) -> None:
        await self.executor.aclose()
        await self.driver.aclose()


def build_executor_host(engine: AsyncEngine) -> ExecutorHost | None:
    """按开关构建宿主;未启用时返回 None(API 只创建 Run,不推进)。"""
    if not e2e_executor_enabled():
        return None
    return ExecutorHost(engine)


def notify_run(app: Any, run_id: Any) -> None:
    """唤醒执行器处理某个 Run;无执行宿主时为空操作。"""
    executor = getattr(app.state, "executor", None)
    if isinstance(executor, RunExecutor):
        executor.submit(run_id)
