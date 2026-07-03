"""Консультация, созданная из брони типа CONSULTATION.

Revision ID: 0018_consultation_source_booking
Revises: 0017_booking_planned_service_master_start
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_consultation_source_booking"
down_revision = "0017_booking_planned_service_master_start"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "consultations",
        sa.Column("source_booking_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_consultations_source_booking_id",
        "consultations",
        "bookings",
        ["source_booking_id"],
        ["id"],
    )
    op.create_index(
        "ix_consultations_source_booking_id",
        "consultations",
        ["source_booking_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_consultations_source_booking_id", table_name="consultations")
    op.drop_constraint("fk_consultations_source_booking_id", "consultations", type_="foreignkey")
    op.drop_column("consultations", "source_booking_id")
