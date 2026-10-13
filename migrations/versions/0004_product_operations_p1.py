"""product operations P1: inventory locations and publication state

P1 设计 §7 的数据模型变更:

- Listing 状态从第一版 ``ACTIVE/INACTIVE`` 迁移为 ``PUBLISHED/UNPUBLISHED``;
  历史行做显式映射(``ACTIVE→PUBLISHED``、``INACTIVE→UNPUBLISHED``),不静默
  丢弃,再重建 ``ck_listings_status`` CHECK;
- 新增 ``fulfillment_locations``(店铺履约位置)与 ``inventory_levels``
  (位置-商品可用库存),后者带 ``available_quantity >= 0`` CHECK 与
  ``(workspace_id, location_id, product_id)`` 唯一约束。

``downgrade()`` 先删除新表,再把发布状态反向映射为 ``ACTIVE/INACTIVE`` 并重建
旧 CHECK。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "product_operations"


def upgrade() -> None:
    # 1) 迁移 Listing 发布状态:先移除旧 CHECK,扩宽列(0002 建的
    #    VARCHAR(8) 装不下 'PUBLISHED'/'UNPUBLISHED'),显式映射历史值,再重建。
    op.drop_constraint("ck_listings_status", "listings", schema=SCHEMA, type_="check")
    op.alter_column(
        "listings",
        "status",
        existing_type=sa.String(length=8),
        type_=sa.String(length=11),
        existing_nullable=False,
        server_default="PUBLISHED",
        schema=SCHEMA,
    )
    op.execute(
        f"UPDATE {SCHEMA}.listings SET status='PUBLISHED' WHERE status='ACTIVE'"
    )
    op.execute(
        f"UPDATE {SCHEMA}.listings SET status='UNPUBLISHED' WHERE status='INACTIVE'"
    )
    op.create_check_constraint(
        "ck_listings_status",
        "listings",
        "status IN ('PUBLISHED', 'UNPUBLISHED')",
        schema=SCHEMA,
    )

    # 2) 履约位置。
    op.create_table(
        "fulfillment_locations",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("store_id", sa.Uuid(), nullable=False),
        sa.Column("external_location_ref", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "aliases",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "DISABLED",
                name="ck_fulfillment_locations_status",
                native_enum=False,
                create_constraint=True,
            ),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["public.workspaces.id"], name="fk_fulfillment_locations_workspace_id"
        ),
        sa.ForeignKeyConstraint(
            ["store_id"], [f"{SCHEMA}.stores.id"], name="fk_fulfillment_locations_store_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "store_id",
            "external_location_ref",
            name="uq_fulfillment_locations_workspace_store_ref",
        ),
        schema=SCHEMA,
    )

    # 3) 位置-商品库存。
    op.create_table(
        "inventory_levels",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("available_quantity", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "available_quantity >= 0", name="ck_inventory_levels_available_quantity"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["public.workspaces.id"], name="fk_inventory_levels_workspace_id"
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            [f"{SCHEMA}.fulfillment_locations.id"],
            name="fk_inventory_levels_location_id",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], [f"{SCHEMA}.products.id"], name="fk_inventory_levels_product_id"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "location_id",
            "product_id",
            name="uq_inventory_levels_workspace_location_product",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("inventory_levels", schema=SCHEMA)
    op.drop_table("fulfillment_locations", schema=SCHEMA)

    op.drop_constraint("ck_listings_status", "listings", schema=SCHEMA, type_="check")
    op.execute(
        f"UPDATE {SCHEMA}.listings SET status='ACTIVE' WHERE status='PUBLISHED'"
    )
    op.execute(
        f"UPDATE {SCHEMA}.listings SET status='INACTIVE' WHERE status='UNPUBLISHED'"
    )
    op.alter_column(
        "listings",
        "status",
        existing_type=sa.String(length=11),
        type_=sa.String(length=8),
        existing_nullable=False,
        server_default="ACTIVE",
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_listings_status",
        "listings",
        "status IN ('ACTIVE', 'INACTIVE')",
        schema=SCHEMA,
    )
