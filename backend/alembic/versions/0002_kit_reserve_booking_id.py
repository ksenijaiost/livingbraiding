"""Резерв комплекта ↔ бронь; визит — флаг «комплект уже оплачен»; фикс 16 — остатки по видам.

Revision ID: 0002_kit_reserve_booking
Revises: 0001_init
Create Date: 2026-05-12

Фикс 16: таблица kit_blank_stock, kit_reserves.kit_key, usage_breakdown_json, kit_breakdown_json.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_kit_reserve_booking"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kit_reserves", sa.Column("booking_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_kit_reserves_booking_id",
        "kit_reserves",
        "bookings",
        ["booking_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "visits",
        sa.Column("kit_paid_separately", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.alter_column("visits", "kit_paid_separately", server_default=None)

    op.create_table(
        "kit_blank_stock",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, primary_key=True),
        sa.Column("kit_id", sa.Integer(), nullable=False),
        sa.Column("kit_key", sa.String(length=80), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["kit_id"], ["kits.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("kit_id", "kit_key", name="uq_kit_blank_stock_kit_key"),
    )
    op.create_index("ix_kit_blank_stock_kit_id", "kit_blank_stock", ["kit_id"])

    op.add_column("kit_reserves", sa.Column("kit_key", sa.String(length=80), nullable=True))
    op.create_index("ix_kit_reserves_kit_key", "kit_reserves", ["kit_key"])

    op.add_column("visit_kit_usages", sa.Column("usage_breakdown_json", sa.Text(), nullable=True))
    op.add_column("product_sales", sa.Column("kit_breakdown_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("product_sales", "kit_breakdown_json")
    op.drop_column("visit_kit_usages", "usage_breakdown_json")
    op.drop_index("ix_kit_reserves_kit_key", table_name="kit_reserves")
    op.drop_column("kit_reserves", "kit_key")
    op.drop_index("ix_kit_blank_stock_kit_id", table_name="kit_blank_stock")
    op.drop_table("kit_blank_stock")

    op.drop_column("visits", "kit_paid_separately")
    op.drop_constraint("fk_kit_reserves_booking_id", "kit_reserves", type_="foreignkey")
    op.drop_column("kit_reserves", "booking_id")
