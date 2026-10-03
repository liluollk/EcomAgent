"""product operations domain schema

Creates the independent ``product_operations`` schema that holds the
reference agent business state: stores, products, listings and price locks
(architecture doc section 3). Business constraints:

- stores unique on (workspace_id, platform, external_store_ref);
- products unique on (workspace_id, sku);
- listings unique on (workspace_id, store_id, product_id);
- prices use Numeric(18, 2) with an explicit currency;
- price lock windows may not overlap per listing (GiST EXCLUDE on
  tstzrange(starts_at, ends_at)), so at most one lock is effective at any
  instant; expired locks do not block new ones.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = 'product_operations'


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS product_operations')
    # btree_gist: GiST operator class for uuid equality used by the
    # price lock exclusion constraint.
    op.execute('CREATE EXTENSION IF NOT EXISTS btree_gist')
    op.create_table('stores',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('platform', sa.Enum('TAOBAO', 'DOUYIN', name='ck_stores_platform', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('external_store_ref', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('aliases', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'DISABLED', name='ck_stores_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['public.workspaces.id'], name='fk_stores_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'platform', 'external_store_ref', name='uq_stores_workspace_platform_ref'),
    schema=SCHEMA
    )
    op.create_table('products',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'DISABLED', name='ck_products_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['public.workspaces.id'], name='fk_products_workspace_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'sku', name='uq_products_workspace_sku'),
    schema=SCHEMA
    )
    op.create_table('listings',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('store_id', sa.Uuid(), nullable=False),
    sa.Column('product_id', sa.Uuid(), nullable=False),
    sa.Column('external_product_ref', sa.String(length=128), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('current_price', sa.Numeric(18, 2), nullable=False),
    sa.Column('cost_price', sa.Numeric(18, 2), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'INACTIVE', name='ck_listings_status', native_enum=False, create_constraint=True), server_default='ACTIVE', nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['public.workspaces.id'], name='fk_listings_workspace_id'),
    sa.ForeignKeyConstraint(['store_id'], [f'{SCHEMA}.stores.id'], name='fk_listings_store_id'),
    sa.ForeignKeyConstraint(['product_id'], [f'{SCHEMA}.products.id'], name='fk_listings_product_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('workspace_id', 'store_id', 'product_id', name='uq_listings_workspace_store_product'),
    schema=SCHEMA
    )
    op.create_table('price_locks',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('listing_id', sa.Uuid(), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=False),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['workspace_id'], ['public.workspaces.id'], name='fk_price_locks_workspace_id'),
    sa.ForeignKeyConstraint(['listing_id'], [f'{SCHEMA}.listings.id'], name='fk_price_locks_listing_id'),
    sa.PrimaryKeyConstraint('id'),
    postgresql.ExcludeConstraint(
        ('listing_id', '='),
        (sa.text('tstzrange(starts_at, ends_at)'), '&&'),
        name='ex_price_locks_listing_window',
        using='gist',
    ),
    schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table('price_locks', schema=SCHEMA)
    op.drop_table('listings', schema=SCHEMA)
    op.drop_table('products', schema=SCHEMA)
    op.drop_table('stores', schema=SCHEMA)
    op.execute('DROP SCHEMA IF EXISTS product_operations')
