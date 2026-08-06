"""Почасовая работа: аннулирование (is_voided).

Revision ID: 0030_hourly_work_void
Revises: 0029_product_sale_percent
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_hourly_work_void"
down_revision = "0029_product_sale_percent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hourly_work_entries",
        sa.Column("is_voided", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("hourly_work_entries", sa.Column("voided_at", sa.DateTime(), nullable=True))
    op.add_column(
        "hourly_work_entries",
        sa.Column("voided_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.alter_column("hourly_work_entries", "is_voided", server_default=None)


def downgrade() -> None:
    op.drop_column("hourly_work_entries", "voided_by_user_id")
    op.drop_column("hourly_work_entries", "voided_at")
    op.drop_column("hourly_work_entries", "is_voided")
