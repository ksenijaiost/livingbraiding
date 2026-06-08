"""Длительность услуги в строке брони.

Revision ID: 0015_booking_planned_service_duration
Revises: 0014_booking_planned_start_datetime
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_booking_planned_service_duration"
down_revision = "0014_booking_planned_start_datetime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "booking_planned_services",
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("booking_planned_services", "duration_minutes")
