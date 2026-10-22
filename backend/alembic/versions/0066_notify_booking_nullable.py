"""Журнал отправок клиенту: бронь и клиент можно отвязать.

Revision ID: 0066_notify_booking_nullable
Revises: 0065_kit_suggest_indexes
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066_notify_booking_nullable"
down_revision = "0065_kit_suggest_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("client_notification_sends") as batch:
        batch.alter_column("booking_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("client_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("client_notification_sends") as batch:
        batch.alter_column("booking_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("client_id", existing_type=sa.Integer(), nullable=False)
