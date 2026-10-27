"""就绪探针:数据库可达 + 执行器循环在运行。

``/api/health`` 只报进程存活,执行器静默退出时它仍然 200。readiness 必须能
区分"进程活着"和"现在真的能推进任务",否则运维侧没有任何信号。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api.main import create_app


class _FakeExecutor:
    def __init__(self, *, healthy: bool, failures: int = 0) -> None:
        self.healthy = healthy
        self.consecutive_failures = failures


async def _get_ready(app) -> tuple[int, dict]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/ready")
    return response.status_code, response.json()


async def test_ready_ok_when_database_and_executor_available(engine: AsyncEngine) -> None:
    app = create_app(engine=engine)
    app.state.executor = _FakeExecutor(healthy=True)

    status, body = await _get_ready(app)

    assert status == 200
    assert body == {"status": "ready", "checks": {"database": "ok", "executor": "ok"}}


async def test_ready_fails_when_executor_stopped(engine: AsyncEngine) -> None:
    """执行器循环退出:进程仍活着,但不能推进任何 Run。"""
    app = create_app(engine=engine)
    app.state.executor = _FakeExecutor(healthy=False)

    status, body = await _get_ready(app)

    assert status == 503
    assert body["checks"]["executor"] == "stopped"
    assert body["checks"]["database"] == "ok"


async def test_ready_reports_degraded_executor(engine: AsyncEngine) -> None:
    """循环还在但连续失败:可用于告警,而不是直接摘流量。"""
    app = create_app(engine=engine)
    app.state.executor = _FakeExecutor(healthy=True, failures=3)

    status, body = await _get_ready(app)

    assert status == 503
    assert body["checks"]["executor"].startswith("degraded")


async def test_ready_fails_when_executor_not_started(engine: AsyncEngine) -> None:
    app = create_app(engine=engine)

    status, body = await _get_ready(app)

    assert status == 503
    assert body["checks"]["executor"] == "not_started"


@pytest.mark.parametrize("path", ["/api/health"])
async def test_liveness_does_not_touch_database(path: str, engine: AsyncEngine) -> None:
    """liveness 保持廉价:数据库不可用时也必须 200,否则会被重启风暴带崩。"""
    app = create_app(engine=engine)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 200
