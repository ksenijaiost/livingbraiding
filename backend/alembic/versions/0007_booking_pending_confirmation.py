"""Статус брони PENDING_CONFIRMATION (ждёт подтверждения).

Revision ID: 0007_booking_pending_confirmation
Revises: 0006_consultations
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op

revision = "0007_booking_pending_confirmation"
down_revision = "0006_consultations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import sqlalchemy as sa

    op.alter_column(
        "bookings",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=False,
    )


def downgrade() -> None:
    import sqlalchemy as sa

    op.alter_column(
        "bookings",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
