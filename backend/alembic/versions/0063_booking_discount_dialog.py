"""Скидка % и «где диалог» у брони (информационно).

Revision ID: 0063_booking_discount_dialog
Revises: 0062_care_memo_before_after
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_booking_discount_dialog"
down_revision = "0062_care_memo_before_after"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("discount_percent", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("dialog_location", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_bookings_discount_percent_0_100",
            "discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100)",
        )


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.drop_constraint("ck_bookings_discount_percent_0_100", type_="check")
        batch.drop_column("dialog_location")
        batch.drop_column("discount_percent")
