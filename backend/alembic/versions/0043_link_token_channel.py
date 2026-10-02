"""Статус channel у токенов привязки (telegram / vk).

Revision ID: 0043_link_token_channel
Revises: 0042_notification_outbox_sending
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_link_token_channel"
down_revision = "0042_notification_outbox_sending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_link_tokens",
        sa.Column("channel", sa.String(length=16), nullable=False, server_default="telegram"),
    )
    op.create_index("ix_telegram_link_tokens_channel", "telegram_link_tokens", ["channel"])


def downgrade() -> None:
    op.drop_index("ix_telegram_link_tokens_channel", table_name="telegram_link_tokens")
    op.drop_column("telegram_link_tokens", "channel")
