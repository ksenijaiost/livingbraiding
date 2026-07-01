"""Индивидуальное время старта мастера в строке брони.

Revision ID: 0017_booking_planned_service_master_start
Revises: 0016_master_time_blocks
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_booking_planned_service_master_start"
down_revision = "0016_master_time_blocks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_planned_service_masters",
        sa.Column("planned_start_time", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("booking_planned_service_masters", "planned_start_time")
