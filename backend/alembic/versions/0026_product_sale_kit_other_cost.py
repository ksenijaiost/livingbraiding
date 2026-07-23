"""Комментарий к комплекту и себестоимость «Другое» в продаже товаров.

Revision ID: 0026_product_sale_kit_other_cost
Revises: 0025_hourly_work_plan_link
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_product_sale_kit_other_cost"
down_revision = "0025_hourly_work_plan_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("product_sales", sa.Column("kit_description", sa.Text(), nullable=True))
    op.add_column("product_sales", sa.Column("other_cost", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("product_sales", "other_cost")
    op.drop_column("product_sales", "kit_description")
