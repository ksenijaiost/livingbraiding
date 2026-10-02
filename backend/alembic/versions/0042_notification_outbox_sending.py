"""Статус sending + locked_at для безопасной обработки outbox несколькими процессами.

Revision ID: 0042_notification_outbox_sending
Revises: 0041_telegram_link_tokens
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_notification_outbox_sending"
down_revision = "0041_telegram_link_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("locked_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_outbox", "locked_at")
