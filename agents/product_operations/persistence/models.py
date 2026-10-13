"""``product_operations`` Schema 下的 SQLAlchemy 模型(架构文档 §3)。

业务 Schema 与 Runtime 核心表(public)分离:独立 ``DeclarativeBase`` 将
metadata schema 固定为 ``product_operations``;``workspace_id`` 外键指向
``public.workspaces.id``,禁止跨 Workspace 引用。约定与 runtime 一致:
UUID 主键、UTC ``timestamptz``、JSONB、非原生枚举(VARCHAR + CHECK)、
价格 ``Numeric(18, 2)``。
"""

import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, MetaData, Uuid
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agents.product_operations.domain.models import (
    ListingStatus,
    ProductStatus,
    StorePlatform,
    StoreStatus,
)
from runtime.persistence.base import (
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    db_enum,
)


class ProductOperationsBase(DeclarativeBase):
    """product_operations Schema 下全部表的声明基类。"""

    metadata = MetaData(schema="product_operations")


class Store(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    """店铺;(workspace_id, platform, external_store_ref) 唯一。"""

    __tablename__ = "stores"

    # workspace_id 的外键(public.workspaces.id)由迁移 DDL 兜底:跨 MetaData
    # 的引用无法在独立 metadata 内解析,ORM 侧声明为普通列。
    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    platform: Mapped[StorePlatform] = mapped_column(
        db_enum(StorePlatform, "ck_stores_platform"), nullable=False
    )
    external_store_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        postgresql.JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    status: Mapped[StoreStatus] = mapped_column(
        db_enum(StoreStatus, "ck_stores_status"),
        nullable=False,
        server_default="ACTIVE",
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "platform",
            "external_store_ref",
            name="uq_stores_workspace_platform_ref",
        ),
    )


class Product(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    """商品;(workspace_id, sku) 唯一。"""

    __tablename__ = "products"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    sku: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[ProductStatus] = mapped_column(
        db_enum(ProductStatus, "ck_products_status"),
        nullable=False,
        server_default="ACTIVE",
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "sku", name="uq_products_workspace_sku"),
    )


class Listing(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    """渠道售卖;(workspace_id, store_id, product_id) 唯一。

    ``current_price``/``cost_price`` 使用 ``Numeric(18, 2)`` + ``Decimal``;
    ``version`` 由 Repository 做 CAS 递增。
    """

    __tablename__ = "listings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("product_operations.stores.id", name="fk_listings_store_id"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("product_operations.products.id", name="fk_listings_product_id"),
        nullable=False,
    )
    external_product_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(sa.Numeric(18, 2), nullable=False)
    cost_price: Mapped[Decimal] = mapped_column(sa.Numeric(18, 2), nullable=False)
    status: Mapped[ListingStatus] = mapped_column(
        db_enum(ListingStatus, "ck_listings_status"),
        nullable=False,
        server_default="PUBLISHED",
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "store_id",
            "product_id",
            name="uq_listings_workspace_store_product",
        ),
    )


class FulfillmentLocation(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    """履约位置;(workspace_id, store_id, external_location_ref) 唯一。"""

    __tablename__ = "fulfillment_locations"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    store_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "product_operations.stores.id", name="fk_fulfillment_locations_store_id"
        ),
        nullable=False,
    )
    external_location_ref: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        postgresql.JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    status: Mapped[StoreStatus] = mapped_column(
        db_enum(StoreStatus, "ck_fulfillment_locations_status"),
        nullable=False,
        server_default="ACTIVE",
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "store_id",
            "external_location_ref",
            name="uq_fulfillment_locations_workspace_store_ref",
        ),
    )


class InventoryLevel(ProductOperationsBase, UUIDPrimaryKeyMixin, TimestampMixin):
    """位置-商品可用库存;(workspace_id, location_id, product_id) 唯一。

    ``available_quantity >= 0`` 由数据库 CHECK 兜底:应用层 CAS 已排除负结果,
    但绕过 ORM 的写入也必须被拒绝。
    """

    __tablename__ = "inventory_levels"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "product_operations.fulfillment_locations.id",
            name="fk_inventory_levels_location_id",
        ),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("product_operations.products.id", name="fk_inventory_levels_product_id"),
        nullable=False,
    )
    available_quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        sa.CheckConstraint(
            "available_quantity >= 0", name="ck_inventory_levels_available_quantity"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "location_id",
            "product_id",
            name="uq_inventory_levels_workspace_location_product",
        ),
    )


class PriceLock(ProductOperationsBase, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """锁价窗口;同一 Listing 的锁价窗口不得重叠(GiST EXCLUDE,见下)。

    ``now()`` 不可用于索引谓词(PostgreSQL 要求 IMMUTABLE),因此以
    ``EXCLUDE USING gist (listing_id WITH =, tstzrange(starts_at, ends_at)
    WITH &&)`` 表达"任一时刻至多一个生效锁":半开区间 ``[starts_at,
    ends_at)`` 下,重叠窗口被数据库拒绝,过期锁不限制新锁。
    """

    __tablename__ = "price_locks"

    workspace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    listing_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("product_operations.listings.id", name="fk_price_locks_listing_id"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))

    __table_args__ = (
        postgresql.ExcludeConstraint(
            ("listing_id", "="),
            (sa.func.tstzrange(sa.text("starts_at"), sa.text("ends_at")), "&&"),
            name="ex_price_locks_listing_window",
            using="gist",
        ),
    )


__all__ = [
    "FulfillmentLocation",
    "InventoryLevel",
    "Listing",
    "PriceLock",
    "Product",
    "ProductOperationsBase",
    "Store",
]
