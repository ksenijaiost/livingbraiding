"""Краткие названия категорий, подкатегорий и услуг (Fix 108).

Revision ID: 0020_catalog_short_names
Revises: 0019_client_payment_kind
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_catalog_short_names"
down_revision = "0019_client_payment_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_categories", sa.Column("short_name", sa.String(length=120), nullable=True))
    op.add_column("service_subcategories", sa.Column("short_name", sa.String(length=160), nullable=True))
    op.add_column("services", sa.Column("short_name", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("services", "short_name")
    op.drop_column("service_subcategories", "short_name")
    op.drop_column("service_categories", "short_name")
