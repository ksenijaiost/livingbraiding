"""Связь резерва комплекта с бронью (для UI и снятия при отмене/смене).

Revision ID: 0002_kit_reserve_booking
Revises: 0001_init
Create Date: 2026-05-12
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


def downgrade() -> None:
    op.drop_constraint("fk_kit_reserves_booking_id", "kit_reserves", type_="foreignkey")
    op.drop_column("kit_reserves", "booking_id")
