"""商品运营 Agent 的只读 Tool(计划 03 Task 2;架构文档 §4.1-4.3)。

- ``definitions.py`` 提供三个 READ ToolDefinition(effect=READ、
  ``product.read``、模型派生的 JSON Schema 与 canonical schema_hash);
- 三个 handler 接收 ``ActorContext`` + 参数 + ``AsyncSession``,查询始终
  携带 workspace_id 与授权 Store ID 集合,输出按 Output Schema 白名单
  构造;
- ``ToolBinding`` 只在 Worker 进程内连接定义与 handler。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.tools.compare_listing_prices import (
    COMPARE_LISTING_PRICES_BINDING,
)
from agents.product_operations.tools.compare_listing_prices import (
    compare_listing_prices as _compare_listing_prices,
)
from agents.product_operations.tools.definitions import (
    COMPARE_LISTING_PRICES_DEFINITION,
    GET_PRODUCT_SNAPSHOT_DEFINITION,
    SEARCH_PRODUCTS_DEFINITION,
)
from agents.product_operations.tools.errors import (
    ProductNotFoundError,
    ToolAccessError,
)
from agents.product_operations.tools.get_product_snapshot import (
    GET_PRODUCT_SNAPSHOT_BINDING,
)
from agents.product_operations.tools.get_product_snapshot import (
    get_product_snapshot as _get_product_snapshot,
)
from agents.product_operations.tools.schemas import (
    CompareListingPricesResult,
    ProductSearchResult,
    ProductSnapshot,
)
from agents.product_operations.tools.search_products import (
    SEARCH_PRODUCTS_BINDING,
)
from agents.product_operations.tools.search_products import (
    search_products as _search_products,
)
from runtime.access.actor import ActorContext


@dataclass(frozen=True)
class ReadTools:
    """三个只读商品 Tool 的进程内组合视图:绑定 Session 后暴露 handler。"""

    session: AsyncSession

    async def search_products(
        self,
        *,
        actor: ActorContext,
        query: str | None = None,
        store_ids: Sequence[str] = (),
    ) -> ProductSearchResult:
        return await _search_products(
            actor=actor,
            session=self.session,
            query=query,
            store_ids=store_ids,
        )

    async def get_product_snapshot(
        self,
        *,
        actor: ActorContext,
        store_id: str,
        sku: str,
    ) -> ProductSnapshot:
        return await _get_product_snapshot(
            actor=actor,
            session=self.session,
            store_id=store_id,
            sku=sku,
        )

    async def compare_listing_prices(
        self,
        *,
        actor: ActorContext,
        sku: str,
        store_ids: Sequence[str] = (),
    ) -> CompareListingPricesResult:
        return await _compare_listing_prices(
            actor=actor,
            session=self.session,
            sku=sku,
            store_ids=store_ids,
        )


__all__ = [
    "COMPARE_LISTING_PRICES_BINDING",
    "COMPARE_LISTING_PRICES_DEFINITION",
    "GET_PRODUCT_SNAPSHOT_BINDING",
    "GET_PRODUCT_SNAPSHOT_DEFINITION",
    "SEARCH_PRODUCTS_BINDING",
    "SEARCH_PRODUCTS_DEFINITION",
    "ProductNotFoundError",
    "ReadTools",
    "ToolAccessError",
]
