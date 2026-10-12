"""商品域测试夹具(计划 03 Task 1;P1 Task 4 扩展库存位置)。

``ProductOperationsSeeder`` 为每个用例创建独立 Workspace 与完整业务状态
(Store/Product/Listing/PriceLock/FulfillmentLocation/InventoryLevel),全部写入
未提交事务,用例结束回滚隔离,参照 ``tests/persistence/conftest.py`` 模式。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from agents.product_operations.domain.models import StorePlatform
from agents.product_operations.persistence.models import (
    FulfillmentLocation,
    InventoryLevel,
    Listing,
    PriceLock,
    Product,
    Store,
)
from runtime.persistence.models.identity import Workspace


DEFAULT_CURRENT_PRICE = Decimal("128.00")
DEFAULT_COST_PRICE = Decimal("88.00")
DEFAULT_CURRENCY = "CNY"
DEFAULT_AVAILABLE_QUANTITY = 20


@dataclass(frozen=True)
class ProductOperationsSeed:
    """单个用例的商品域种子行。"""

    workspace: Workspace
    store: Store
    product: Product
    listing: Listing
    price_lock: PriceLock
    fulfillment_location: FulfillmentLocation
    inventory_level: InventoryLevel


class ProductOperationsSeeder:
    """在未提交事务内创建每用例独立的商品域种子。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def seed(
        self,
        *,
        platform: StorePlatform = StorePlatform.TAOBAO,
        current_price: Decimal = DEFAULT_CURRENT_PRICE,
        cost_price: Decimal = DEFAULT_COST_PRICE,
        currency: str = DEFAULT_CURRENCY,
    ) -> ProductOperationsSeed:
        """创建独立 Workspace + Store/Product/Listing/PriceLock 并 flush。"""
        now = datetime.now(tz=UTC)
        workspace = Workspace(id=uuid.uuid4(), name=f"ws-{uuid.uuid4()}")
        store = Store(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            platform=platform,
            external_store_ref=f"shop-{uuid.uuid4()}",
            name="seed-store",
            aliases=[],
        )
        product = Product(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            sku=f"SKU-{uuid.uuid4()}",
            title="seed-product",
        )
        listing = Listing(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            store_id=store.id,
            product_id=product.id,
            external_product_ref=f"item-{uuid.uuid4()}",
            currency=currency,
            current_price=current_price,
            cost_price=cost_price,
        )
        price_lock = PriceLock(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            listing_id=listing.id,
            reason="seed-active-lock",
            starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=7),
        )
        fulfillment_location = FulfillmentLocation(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            store_id=store.id,
            external_location_ref=f"loc-{uuid.uuid4()}",
            name="seed-location",
            aliases=[],
        )
        inventory_level = InventoryLevel(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            location_id=fulfillment_location.id,
            product_id=product.id,
            available_quantity=DEFAULT_AVAILABLE_QUANTITY,
        )
        # 无 relationship() 声明,SQLAlchemy 不保证跨表插入顺序;
        # 按外键依赖分批 flush,保证被引用行先落库。
        self._session.add(workspace)
        await self._session.flush()
        self._session.add_all([store, product])
        await self._session.flush()
        self._session.add(listing)
        await self._session.flush()
        self._session.add_all([price_lock, fulfillment_location])
        await self._session.flush()
        self._session.add(inventory_level)
        await self._session.flush()
        # 载入 server default(version/updated_at),供 CAS 测试断言。
        await self._session.refresh(listing)
        await self._session.refresh(inventory_level)
        return ProductOperationsSeed(
            workspace=workspace,
            store=store,
            product=product,
            listing=listing,
            price_lock=price_lock,
            fulfillment_location=fulfillment_location,
            inventory_level=inventory_level,
        )
