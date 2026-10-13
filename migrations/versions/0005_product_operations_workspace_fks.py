"""product operations: composite workspace ownership foreign keys

第一版把 ``workspace_id`` 与业务父键写成两条独立外键,因此 "同 Workspace" 只是
应用层约定,而不是数据库不变量:Workspace A 的 listing 可以引用 Workspace B 的
store,Mock 平台状态、回查与审计随之被污染。本迁移把归属关系下沉为数据库约束:

- 父表(``stores`` / ``products`` / ``listings`` / ``fulfillment_locations``)
  补 ``(workspace_id, id)`` 唯一键;
- 子表外键改为组合 ``(workspace_id, <parent>_id) → <parent>(workspace_id, id)``,
  使跨 Workspace 引用被数据库直接拒绝。

约束组合 FK 前先校验现有数据:若已有跨 Workspace 引用,升级 fail closed 并给出
可定位的错误,而不是让 DDL 抛出难读的完整性错误。

``downgrade()`` 反向恢复单列外键并删除 ``(workspace_id, id)`` 唯一键。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "product_operations"

# 需要 (workspace_id, id) 唯一键的父表(供组合外键引用)。
_PARENT_TABLES: tuple[str, ...] = (
    "stores",
    "products",
    "listings",
    "fulfillment_locations",
)

# (子表, 新约束名, 旧约束名, 子列, 父表, 父键列)
_COMPOSITE_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    ("listings", "fk_listings_store_id", "fk_listings_store", "store_id", "stores"),
    ("listings", "fk_listings_product_id", "fk_listings_product", "product_id", "products"),
    (
        "fulfillment_locations",
        "fk_fulfillment_locations_store_id",
        "fk_fulfillment_locations_store",
        "store_id",
        "stores",
    ),
    (
        "inventory_levels",
        "fk_inventory_levels_location_id",
        "fk_inventory_levels_location",
        "location_id",
        "fulfillment_locations",
    ),
    (
        "inventory_levels",
        "fk_inventory_levels_product_id",
        "fk_inventory_levels_product",
        "product_id",
        "products",
    ),
    (
        "price_locks",
        "fk_price_locks_listing_id",
        "fk_price_locks_listing",
        "listing_id",
        "listings",
    ),
)


def upgrade() -> None:
    _assert_no_cross_workspace_references()

    for table in _PARENT_TABLES:
        op.create_unique_constraint(
            f"uq_{table}_workspace_id", table, ["workspace_id", "id"], schema=SCHEMA
        )

    for child_table, old_name, new_name, child_col, parent_table in _COMPOSITE_FOREIGN_KEYS:
        op.drop_constraint(old_name, child_table, schema=SCHEMA, type_="foreignkey")
        op.create_foreign_key(
            new_name,
            child_table,
            parent_table,
            ["workspace_id", child_col],
            ["workspace_id", "id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
        )


def downgrade() -> None:
    for child_table, old_name, new_name, child_col, parent_table in reversed(
        _COMPOSITE_FOREIGN_KEYS
    ):
        op.drop_constraint(new_name, child_table, schema=SCHEMA, type_="foreignkey")
        op.create_foreign_key(
            old_name,
            child_table,
            parent_table,
            [child_col],
            ["id"],
            source_schema=SCHEMA,
            referent_schema=SCHEMA,
        )

    for table in _PARENT_TABLES:
        op.drop_constraint(f"uq_{table}_workspace_id", table, schema=SCHEMA, type_="unique")


def _assert_no_cross_workspace_references() -> None:
    """组合外键生效前,显式检查是否已存在跨 Workspace 引用(存在则 fail closed)。"""
    connection = op.get_bind()
    for child_table, _old, _new, child_col, parent_table in _COMPOSITE_FOREIGN_KEYS:
        conflicts = connection.scalar(
            sa.text(
                f"SELECT count(*) FROM {SCHEMA}.{child_table} AS c "
                f"LEFT JOIN {SCHEMA}.{parent_table} AS p "
                f"ON p.id = c.{child_col} AND p.workspace_id = c.workspace_id "
                f"WHERE p.id IS NULL"
            )
        )
        if conflicts:
            raise RuntimeError(
                f"Cannot add composite workspace foreign key on "
                f"{SCHEMA}.{child_table}.{child_col}: {conflicts} row(s) reference a "
                f"{parent_table} row in another workspace (or a missing row). "
                "Repair those rows before upgrading."
            )
