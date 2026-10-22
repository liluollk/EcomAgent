"""E2E 装配开关测试(阶段 4 Gate;``RUNTIME_E2E=1``)。

端到端验证 apps 层 E2E 装配:ScriptedBackend(按任务表逐步发出 P1 ToolCall)
+ Mock Commerce Adapter(真实写 listings/inventory_levels),覆盖四条 P1
主路径 × AUTO_EXECUTE / REQUIRE_CONFIRMATION 两种执行模式:

    1. 搜索 → 比价 → 设置 Listing 价格 → 回查(写前缺店铺 Input Request);
    2. 读取库存 → 增量调整 → 回查;
    3. 读取 Listing → 发布 → 回查;
    4. 读取 Listing → 取消发布 → 回查。

CONFIRM_WRITES 模式下写前经 API 确认端点接受;AUTO_EXECUTE 模式下写直接
执行。确认/写执行由 Runtime 重新校验权限、Policy 与资源版本(既有语义)。
Run 由 API 进程内的 RunExecutor 推进(应用 lifespan 装配,``RUNTIME_E2E=1``),
路由提交事务后唤醒执行器;API 走完整路由栈,不依赖任何外部真实服务
(模型=Scripted,mem0/langfuse 不启用)。
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from agents.product_operations.agent import AGENT_KEY
from agents.product_operations.domain.models import ListingStatus
from agents.product_operations.persistence.models import InventoryLevel, Listing
from agents.product_operations.skill import SKILL_KEY
from apps.api.main import create_app
from runtime.persistence.base import ExecutionMode


_ALL_TOOL_STABLE_KEYS: Final[tuple[str, ...]] = (
    # P1 当前能力(新 Run 可见)。
    "search_products",
    "compare_listing_prices",
    "set_listing_price",
    "get_listing",
    "publish_listing",
    "unpublish_listing",
    "get_inventory_level",
    "adjust_inventory_quantity",
    # 遗留兼容能力(仅旧 Version ID 恢复可见)。
    "get_product_snapshot",
    "update_price",
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
    # 进入应用 lifespan:装配并启动进程内 Run 执行器(RUNTIME_E2E=1),
    # 路由提交后由执行器推进;退出时释放单写者锁。
    async with app.router.lifespan_context(app), AsyncClient(
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
        "DELETE FROM product_operations.inventory_levels WHERE workspace_id = :ws",
        "DELETE FROM product_operations.price_locks WHERE workspace_id = :ws",
        "DELETE FROM product_operations.listings WHERE workspace_id = :ws",
        "DELETE FROM product_operations.fulfillment_locations WHERE workspace_id = :ws",
        "DELETE FROM product_operations.products WHERE workspace_id = :ws",
        "DELETE FROM product_operations.stores WHERE workspace_id = :ws",
        "DELETE FROM membership_permissions WHERE workspace_id = :ws",
        "DELETE FROM membership_resource_scopes WHERE workspace_id = :ws",
        "DELETE FROM membership_resource_scope_modes WHERE workspace_id = :ws",
        "DELETE FROM model_config_versions WHERE workspace_id = :ws",
        "DELETE FROM model_connections WHERE workspace_id = :ws",
        "DELETE FROM workspace_memberships WHERE workspace_id = :ws",
        "DELETE FROM business_policy_versions WHERE workspace_id = :ws",
        "DELETE FROM business_policies WHERE workspace_id = :ws",
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
                    {"keys": list(_ALL_TOOL_STABLE_KEYS)}
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


async def _wait_status(client: AsyncClient, run_id: str, status: str) -> bool:
    """谓词:Run 已到达目标状态(供 _wait_for 轮询)。"""
    return (await _run_status(client, run_id)) == status


async def _set_execution_mode(
    engine: AsyncEngine, workspace_id: str, mode: ExecutionMode
) -> None:
    """把种子工作区切到目标执行模式(验证 AUTO_EXECUTE 主路径)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await session.execute(
                sa.text(
                    "UPDATE workspaces SET execution_mode = :mode WHERE id = :ws"
                ),
                {"mode": mode.value, "ws": uuid.UUID(workspace_id)},
            )
    finally:
        await session.close()


async def _set_listing_status(
    engine: AsyncEngine, listing_id: str, status: ListingStatus
) -> None:
    """把种子 Listing 切到目标发布状态(发布/下架路径的确定初态)。"""
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        async with session.begin():
            await session.execute(
                sa.update(Listing)
                .where(Listing.id == uuid.UUID(listing_id))
                .values(status=status)
            )
    finally:
        await session.close()


async def _authoritative_price(engine: AsyncEngine, listing_id: str) -> Decimal:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        listing = (
            await session.scalars(
                select(Listing).where(Listing.id == uuid.UUID(listing_id))
            )
        ).one()
        return listing.current_price
    finally:
        await session.close()


async def _authoritative_quantity(
    engine: AsyncEngine, workspace_id: str
) -> int:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        levels = (
            (
                await session.scalars(
                    select(InventoryLevel).where(
                        InventoryLevel.workspace_id == uuid.UUID(workspace_id)
                    )
                )
            )
            .all()
        )
        assert len(levels) == 1, "e2e 种子只有一个库存位置"
        return levels[0].available_quantity
    finally:
        await session.close()


async def _authoritative_publication_state(
    engine: AsyncEngine, listing_id: str
) -> ListingStatus:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        listing = (
            await session.scalars(
                select(Listing).where(Listing.id == uuid.UUID(listing_id))
            )
        ).one()
        return listing.status
    finally:
        await session.close()


async def _login(client: AsyncClient, seed: dict[str, Any]) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"login": seed["login"], "password": seed["password"]},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {"Authorization": f"Bearer {response.json()['access_token']}"}
    )


async def _submit_task(
    client: AsyncClient, seed: dict[str, Any], content: str
) -> tuple[str, str]:
    """创建 Session 并提交任务消息,返回 (session_id, run_id)。"""
    response = await client.post(
        "/api/sessions",
        json={"workspace_id": seed["workspace_id"]},
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    response = await client.post(
        f"/api/sessions/{session_id}/messages",
        json={"content": content},
    )
    assert response.status_code == 201, response.text
    return session_id, response.json()["run_id"]


def _pending_inputs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return snapshot["pending_inputs"]


def _pending_confirmations(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return snapshot["pending_confirmations"]


async def _answer_store_ref(
    client: AsyncClient, run_id: str, session_id: str, store_id: str
) -> None:
    """缺店铺 Input Request:快照给出候选,经 API 端点回答淘宝旗舰店。

    P1 引用契约下候选 ``value`` 是模型可见的店铺引用(名称),不是内部 ID。
    """
    del store_id
    await _wait_for(lambda: _wait_status(client, run_id, "WAITING_INPUT"))
    response = await client.get(f"/api/sessions/{session_id}")
    assert response.status_code == 200, response.text
    store_input = _pending_inputs(response.json())[0]
    assert store_input["field_paths"] == ["store_ref"]
    option = next(
        option
        for option in store_input["options"]
        if option["label"] == "淘宝旗舰店"
    )
    response = await client.post(
        f"/api/requests/{store_input['id']}/answer",
        json={"values": {"store_ref": option["value"]}},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# 价格主路径:搜索 → 比价 → 改价 → 回查
# ---------------------------------------------------------------------------


async def test_e2e_price_path_confirmed_mode(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """CONFIRM_WRITES:输入 → 确认 → 恢复推进后,权威价真实落到 99.00。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]
    await _login(client, seed)
    session_id, run_id = await _submit_task(
        client, seed, "把 SKU-001 改成 99 元"
    )
    await _answer_store_ref(client, run_id, session_id, seed["store_id"])
    await _wait_for(
        lambda: _wait_status(client, run_id, "WAITING_CONFIRMATION")
    )
    response = await client.get(f"/api/sessions/{session_id}")
    confirmation = _pending_confirmations(response.json())[0]
    arguments = confirmation["normalized_arguments"]
    assert arguments["sku"] == "SKU-001"
    assert arguments["store_ref"] == "淘宝旗舰店"
    assert Decimal(str(arguments["target_price"])) == Decimal("99.00")
    response = await client.post(
        f"/api/requests/{confirmation['id']}/confirmation/accept"
    )
    assert response.status_code == 200, response.text
    assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    assert await _authoritative_price(engine, seed["listing_id"]) == Decimal("99.00")


async def test_e2e_price_full_path_auto_mode(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """AUTO_EXECUTE:搜索 → 比价 → 改价 → 回查一次跑通,权威价 99.00。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]
    await _login(client, seed)
    await _set_execution_mode(
        engine, seed["workspace_id"], ExecutionMode.AUTO_EXECUTE
    )
    session_id, run_id = await _submit_task(
        client,
        seed,
        "对比各店铺 SKU-001 的价格，然后把 SKU-001 改成 99 元，再回查一次",
    )
    # 写前仍需补店铺引用(Input Request 与执行模式无关)。
    await _answer_store_ref(client, run_id, session_id, seed["store_id"])
    assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    assert await _authoritative_price(engine, seed["listing_id"]) == Decimal("99.00")


# ---------------------------------------------------------------------------
# 库存主路径:读取 → 增量调整 → 回查
# ---------------------------------------------------------------------------


async def test_e2e_inventory_path_confirmed_mode(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """CONFIRM_WRITES:库存读取 → 调整前确认 → 权威库存 20 + 5 = 25。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]
    await _login(client, seed)
    session_id, run_id = await _submit_task(
        client, seed, "把淘宝旗舰店的 SKU-001 库存补 5 件，前后各回查一次"
    )
    await _wait_for(
        lambda: _wait_status(client, run_id, "WAITING_CONFIRMATION")
    )
    response = await client.get(f"/api/sessions/{session_id}")
    confirmation = _pending_confirmations(response.json())[0]
    arguments = confirmation["normalized_arguments"]
    assert arguments["sku"] == "SKU-001"
    assert arguments["store_ref"] == "淘宝旗舰店"
    assert int(arguments["delta"]) == 5
    assert arguments["reason"] == "RECEIPT"
    response = await client.post(
        f"/api/requests/{confirmation['id']}/confirmation/accept"
    )
    assert response.status_code == 200, response.text
    assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    assert await _authoritative_quantity(engine, seed["workspace_id"]) == 25


async def test_e2e_inventory_path_auto_mode(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """AUTO_EXECUTE:库存读取 → 调整 → 回查一次跑通,权威库存 25。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]
    await _login(client, seed)
    await _set_execution_mode(
        engine, seed["workspace_id"], ExecutionMode.AUTO_EXECUTE
    )
    _, run_id = await _submit_task(
        client, seed, "把淘宝旗舰店的 SKU-001 库存补 5 件，前后各回查一次"
    )
    assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    assert await _authoritative_quantity(engine, seed["workspace_id"]) == 25


# ---------------------------------------------------------------------------
# 刊登主路径:读取 → 发布 / 读取 → 取消发布
# ---------------------------------------------------------------------------


async def test_e2e_publish_path_confirmed_mode(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """CONFIRM_WRITES:未发布 Listing 经确认后发布。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]
    await _set_listing_status(engine, seed["listing_id"], ListingStatus.UNPUBLISHED)
    await _login(client, seed)
    session_id, run_id = await _submit_task(
        client, seed, "上架淘宝旗舰店的 SKU-001"
    )
    await _wait_for(
        lambda: _wait_status(client, run_id, "WAITING_CONFIRMATION")
    )
    response = await client.get(f"/api/sessions/{session_id}")
    confirmation = _pending_confirmations(response.json())[0]
    arguments = confirmation["normalized_arguments"]
    assert arguments["store_ref"] == "淘宝旗舰店"
    assert arguments["sku"] == "SKU-001"
    response = await client.post(
        f"/api/requests/{confirmation['id']}/confirmation/accept"
    )
    assert response.status_code == 200, response.text
    assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    assert (
        await _authoritative_publication_state(engine, seed["listing_id"])
        is ListingStatus.PUBLISHED
    )


async def test_e2e_unpublish_path_auto_mode(
    e2e_environment: dict[str, Any], engine: AsyncEngine
) -> None:
    """AUTO_EXECUTE:已发布 Listing 一次跑通取消发布。"""
    client: AsyncClient = e2e_environment["client"]
    seed: dict[str, Any] = e2e_environment["seed"]
    await _login(client, seed)
    await _set_execution_mode(
        engine, seed["workspace_id"], ExecutionMode.AUTO_EXECUTE
    )
    _, run_id = await _submit_task(
        client, seed, "下架淘宝旗舰店的 SKU-001"
    )
    assert await _wait_for(lambda: _wait_status(client, run_id, "COMPLETED"))
    assert (
        await _authoritative_publication_state(engine, seed["listing_id"])
        is ListingStatus.UNPUBLISHED
    )
