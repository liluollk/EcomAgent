"""``python -m apps.worker`` 进程入口(最小组合根,计划 04 Task 5)。

组装 WorkerSettings → AsyncEngine → TransactionalLeaseGateway → HealthReporter
→ WorkerHost 并运行;SIGTERM/SIGINT 触发优雅停止(仅本入口处理信号)。
Agent 推进回调由 RuntimeAssembly 组合根接入时替换:最小入口的占位回调把
领取到的 Job 按暂时性错误释放(WORKER_ERROR),不执行业务推进。
"""

from __future__ import annotations

from apps.worker.main import main


if __name__ == "__main__":
    main()
