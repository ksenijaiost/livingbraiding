"""Продажа товаров: процент с продажи (10/15) для начисления в ЗП.

Revision ID: 0029_product_sale_percent
Revises: 0028_super_admin_purge_logs
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_product_sale_percent"
down_revision = "0028_super_admin_purge_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "product_sales",
        sa.Column("sale_percent", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("product_sales", "sale_percent")
