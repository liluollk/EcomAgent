"""商品读 Tool 的独立执行端点(计划 04 Task 6;架构文档 §4.1)。

每个读 Tool 一个 Handler:只接受自己 Tool 的参数,不做跨 Tool 的运行时
if/elif 分发。三者共享 ``session_factory`` 与请求级 Actor 构造方式,但不共享
dispatcher——新增读 Tool 只需新增一个 Handler,不改动既有分支。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.tools.compare_listing_prices import (
    compare_listing_prices,
)
from agents.product_operations.tools.get_product_snapshot import get_product_snapshot
from agents.product_operations.tools.search_products import search_products
from runtime.access.actor import ActorContext, ActorContextBuilder
from runtime.execution.tool_result_processor import ToolExecutionReceipt


def _store_ids_of(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """读取多店铺引用;非序列值(含字符串)按空集合处理。"""
    raw = arguments.get("store_ids")
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return tuple(str(value) for value in raw)
    return ()


class _ProductReadHandler:
    """读 Tool Handler 共享骨架:构造请求级 Session 与 Actor,并保证关闭。"""

    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
    ) -> None:
        self._session_factory = session_factory
        self._workspace_id = workspace_id
        self._actor_id = actor_id

    async def read(
        self,
        *,
        tool_call_id: uuid.UUID,
        normalized_arguments: Mapping[str, Any],
        deadline: datetime,
    ) -> ToolExecutionReceipt:
        del deadline  # 读 Tool 超时由 Loop 预算覆盖。
        arguments = dict(normalized_arguments)
        session = self._session_factory()
        try:
            actor = await ActorContextBuilder(session).build(
                actor_id=self._actor_id,
                workspace_id=self._workspace_id,
                request_id=f"read-{uuid.uuid4()}",
            )
            result = await self._invoke(
                actor=actor, session=session, arguments=arguments
            )
            return ToolExecutionReceipt(
                tool_call_id=tool_call_id,
                status="OK",
                payload=result.model_dump(mode="json"),
            )
        finally:
            await session.close()

    async def _invoke(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        arguments: Mapping[str, Any],
    ) -> Any:
        raise NotImplementedError


class SearchProductsHandler(_ProductReadHandler):
    """``search_products`` 执行端点:关键词查询 + 可选多店铺收窄。"""

    async def _invoke(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        arguments: Mapping[str, Any],
    ) -> Any:
        query = arguments.get("query")
        return await search_products(
            actor=actor,
            session=session,
            query=query if isinstance(query, str) else None,
            store_ids=_store_ids_of(arguments),
        )


class GetProductSnapshotHandler(_ProductReadHandler):
    """``get_product_snapshot`` 执行端点:单店铺 + SKU 快照。"""

    async def _invoke(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        arguments: Mapping[str, Any],
    ) -> Any:
        return await get_product_snapshot(
            actor=actor,
            session=session,
            store_id=str(arguments["store_id"]),
            sku=str(arguments.get("sku", "")),
        )


class CompareListingPricesHandler(_ProductReadHandler):
    """``compare_listing_prices`` 执行端点:单 SKU 多店铺比价。"""

    async def _invoke(
        self,
        *,
        actor: ActorContext,
        session: AsyncSession,
        arguments: Mapping[str, Any],
    ) -> Any:
        return await compare_listing_prices(
            actor=actor,
            session=session,
            sku=str(arguments["sku"]),
            store_ids=_store_ids_of(arguments),
        )


__all__ = [
    "CompareListingPricesHandler",
    "GetProductSnapshotHandler",
    "SearchProductsHandler",
]
