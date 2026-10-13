"""E2E 装配开关测试(阶段 4 Gate;``RUNTIME_E2E=1``)。

端到端验证 apps 层 E2E 装配:ScriptedBackend(用户消息触发一次
update_price ToolCall)+ Mock Commerce Adapter(真实写 listings):

    seed(HTTP)→ 登录 → 创建 Session → 提交"把 SKU-001 改成 99 元"
    → Worker 领取推进 → 缺店铺 Input Request → API 回答"淘宝旗舰店"
    → 写前确认 → API 接受 → Worker 恢复推进 → Run COMPLETED
    → 权威价(listings.current_price)= 99.00。

Worker 以真实 WorkerHost + TransactionalLeaseGateway 驱动(与 compose 中
``python -m apps.worker`` 的 RUNTIME_E2E 路径一致),API 走完整路由栈;
不依赖任何外部真实服务(模型=Scripted,mem0/langfuse 不启用)。
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.agent import AGENT_KEY
from agents.product_operations.persistence.models import Listing
from agents.product_operations.skill import SKILL_KEY
from agents.product_operations.tools.compare_listing_prices import (
    COMPARE_LISTING_PRICES_DEFINITION,
)
from agents.product_operations.tools.definitions import (
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.update_price import (
    UPDATE_PRICE_DEFINITION,
)
from apps.api.main import create_app
from apps.worker.e2e import E2EJobHandler
from apps.worker.main import TransactionalLeaseGateway, WorkerHost


_E2E_TOOL_KEYS: Final[tuple[str, ...]] = (
    SEARCH_PRODUCTS_DEFINITION.stable_key,
    GET_PRODUCT_SNAPSHOT_DEFINITION.stable_key,
    COMPARE_LISTING_PRICES_DEFINITION.stable_key,
    UPDATE_PRICE_DEFINITION.stable_key,
)

_POLL_INTERVAL: Final = 0.05
_ADVANCE_TIMEOUT: Final = 20.0


async def _wait_for(predicate: Any, *, timeout: float = _ADVANCE_TIMEOUT) -> Any:
    """轮询等待异步谓词为真;超时抛 AssertionError(而非无限悬挂)。"""
    deadline = time.monotonic() + timeout
    while True:
        result = await predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached within timeout")
        await asyncio.sleep(_POLL_INTERVAL)


@pytest.fixture()
async def e2e_environment(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    """RUNTIME_E2E=1 的 API 应用 + 幂等种子;结束按外键顺序清理提交行。"""
    monkeypatch.setenv("RUNTIME_E2E", "1")
    app = create_app(engine=engine)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/e2e/seed")
        assert response.status_code == 200, response.text
        seed: dict[str, Any] = response.json()
        yield {"client": client, "seed": seed}
    await _cleanup_seeded_rows(engine, seed)


async def _cleanup_seeded_rows(engine: AsyncEngine, seed: dict[str, Any]) -> None:
    """删除种子工作区/账号与全局能力行(幂等;按外键依赖排序)。"""
    statements: Final[tuple[str, ...]] = (
        "DELETE FROM audit_events WHERE workspace_id = :ws",
        "DELETE FROM agent_events WHERE workspace_id = :ws",
        "DELETE FROM operation_attempts WHERE workspace_id = :ws",
        "UPDATE tool_calls SET operation_id = NULL WHERE workspace_id = :ws",
        "DELETE FROM confirmation_requests WHERE workspace_id = :ws",
        "DELETE FROM input_requests WHERE workspace_id = :ws",
        "DELETE FROM operations WHERE workspace_id = :ws",
        "DELETE FROM tool_calls WHERE workspace_id = :ws",
        "DELETE FROM run_attempts WHERE workspace_id = :ws",
        "DELETE FROM run_jobs WHERE workspace_id = :ws",
        "UPDATE messages SET run_id = NULL WHERE workspace_id = :ws",
        "UPDATE runs SET trigger_message_id = NULL WHERE workspace_id = :ws",
        "DELETE FROM messages WHERE workspace_id = :ws",
        "DELETE FROM run_tool_versions WHERE workspace_id = :ws",
        "DELETE FROM run_skill_versions WHERE workspace_id = :ws",
        "DELETE FROM run_source_versions WHERE workspace_id = :ws",
        "DELETE FROM run_policy_snapshots WHERE workspace_id = :ws",
        "DELETE FROM runs WHERE workspace_id = :ws",
        "DELETE FROM sessions WHERE workspace_id = :ws",
        "DELETE FROM workspace_agent_bindings WHERE workspace_id = :ws",
        "DELETE FROM product_operations.price_locks WHERE workspace_id = :ws",
        "DELETE FROM product_operations.listings WHERE workspace_id = :ws",
        "DELETE FROM product_operations.products WHERE workspace_id = :ws",
        "DELETE FROM product_operations.stores WHERE workspace_id = :ws",
        "DELETE FROM membership_permissions WHERE workspace_id = :ws",
        "DELETE FROM membership_resource_scopes WHERE workspace_id = :ws",
        "DELETE FROM membership_resource_scope_modes WHERE workspace_id = :ws",
        "DELETE FROM model_config_versions WHERE workspace_id = :ws",
        "DELETE FROM model_connections WHERE workspace_id = :ws",
        "DELETE FROM workspace_memberships WHERE workspace_id = :ws",
        "DELETE FROM workspaces WHERE id = :ws",
        "DELETE FROM users WHERE login = 'e2e-operator'",
    )
    capability_statements: Final[tuple[str, str, str]] = (
        (
            ("DELETE FROM tool_versions WHERE workspace_id IS NULL AND tool_id IN "
            "(SELECT id FROM tool_definitions WHERE scope = 'GLOBAL' "
            "AND workspace_id IS NULL AND stable_key = ANY(:keys))"),
            ("DELETE FROM tool_definitions WHERE scope = 'GLOBAL' "
            "AND workspace_id IS NULL AND stable_key = ANY(:keys)"),
            "keys",
        ),
        (
            ("DELETE FROM skill_versions WHERE workspace_id IS NULL AND skill_id IN "
            "(SELECT id FROM skills WHERE stable_key = :skill_key)"),
            "DELETE FROM skills WHERE stable_key = :skill_key",
            "skill_key",
        ),
        (
            ("DELETE FROM agent_versions WHERE agent_id IN "
            "(SELECT id FROM agents WHERE stable_key = :agent_key)"),
            "DELETE FROM agents WHERE stable_key = :agent_key",
            "agent_key",
        ),
    )
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            for text in statements:
                await session.execute(sa.text(text), {"ws": seed["workspace_id"]})
            for version_text, definition_text, key_name in capability_statements:
                params: dict[str, Any] = (
                    {"keys": list(_E2E_TOOL_KEYS)}
                    if key_name == "keys"
                    else {"skill_key": SKILL_KEY}
                    if key_name == "skill_key"
                    else {"agent_key": AGENT_KEY}
                )
                await session.execute(sa.text(version_text), params)
                await session.execute(sa.text(definition_text), params)
    finally:
        await session.close()


async def _run_status(client: AsyncClient, run_id: str) -> str:
    response = await client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


async def test_e2e_flow_reaches_authoritative_price(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """输入 → 确认 → 恢复推进后,权威价经 Mock Adapter 真实落到 99.00。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]

    # 登录(真实密码校验,非测试注入 token)。
    response = await client.post(
        "/api/auth/login",
        json={"login": seed["login"], "password": seed["password"]},
    )
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    client.headers.update(headers)

    # Worker:与 compose 中 RUNTIME_E2E=1 的 python -m apps.worker 同路径。
    handler = E2EJobHandler(engine, worker_id="e2e-test-worker")
    host = WorkerHost(
        gateway=TransactionalLeaseGateway(engine),
        handler=handler,
        worker_id="e2e-test-worker",
        heartbeat_interval=timedelta(seconds=5),
        poll_interval=timedelta(milliseconds=20),
    )
    await host.start()
    try:
        # 创建 Session 并提交任务(工作台同款命令)。
        response = await client.post(
            "/api/sessions",
            json={"workspace_id": seed["workspace_id"]},
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
        response = await client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "把 SKU-001 改成 99 元"},
        )
        assert response.status_code == 201, response.text
        run_id = response.json()["run_id"]

        # 缺店铺:Run 停在 WAITING_INPUT,快照给出店铺候选。
        await _wait_for(lambda: _wait_status(client, run_id, "WAITING_INPUT"))
        response = await client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        pending_inputs = snapshot["pending_inputs"]
        assert pending_inputs, "expected a pending store input request"
        store_input = pending_inputs[0]
        assert store_input["field_paths"] == ["store_id"]
        option = next(
            option
            for option in store_input["options"]
            if option["label"] == seed["store_name"]
        )
        assert option["value"] == seed["store_id"]

        # 回答 Input Request(API 端点),Run 恢复推进到写前确认。
        response = await client.post(
            f"/api/requests/{store_input['id']}/answer",
            json={"values": {"store_id": option["value"]}},
        )
        assert response.status_code == 200, response.text
        await _wait_for(lambda: _wait_status(client, run_id, "WAITING_CONFIRMATION"))
        response = await client.get(f"/api/sessions/{session_id}")
        pending_confirmations = response.json()["pending_confirmations"]
        assert pending_confirmations, "expected a pending write confirmation"
        confirmation = pending_confirmations[0]
        arguments = confirmation["normalized_arguments"]
        assert arguments["sku"] == "SKU-001"
        assert arguments["store_id"] == seed["store_id"]
        assert Decimal(str(arguments["target_price"])) == Decimal("99.00")

        # 接受确认(单次消费),恢复推进后 Run COMPLETED。
        response = await client.post(
            f"/api/requests/{confirmation['id']}/confirmation/accept"
        )
        assert response.status_code == 200, response.text
        assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    finally:
        await host.request_shutdown()
        await host.wait_stopped(timeout=10.0)
        await handler.aclose()

    # 权威价:经 MockCommerceAdapter 真实写入的 listings.current_price。
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        listing = (
            await session.scalars(
                select(Listing).where(Listing.id == uuid.UUID(seed["listing_id"]))
            )
        ).one_or_none()
        assert listing is not None
        assert listing.current_price == Decimal("99.00")
    finally:
        await session.close()


async def _wait_status(client: AsyncClient, run_id: str, status: str) -> bool:
    """谓词:Run 已到达目标状态(供 _wait_for 轮询)。"""
    return (await _run_status(client, run_id)) == status
