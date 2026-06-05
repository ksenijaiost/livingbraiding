"""Комментарий к услуге в брони.

Revision ID: 0012_booking_planned_service_comment
Revises: 0011_user_salon_cut_override
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_booking_planned_service_comment"
down_revision = "0011_user_salon_cut_override"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("booking_planned_services", sa.Column("comment", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("booking_planned_services", "comment")

