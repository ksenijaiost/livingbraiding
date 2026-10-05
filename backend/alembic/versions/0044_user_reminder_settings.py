"""Таблица user_reminder_settings + флаг reminders_configured у users.

Revision ID: 0044_user_reminder_settings
Revises: 0043_link_token_channel
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_user_reminder_settings"
down_revision = "0043_link_token_channel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("reminders_configured", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "user_reminder_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("minutes_before", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "minutes_before", name="uq_user_reminder_settings_user_minutes"),
    )
    op.create_index("ix_user_reminder_settings_user_id", "user_reminder_settings", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_reminder_settings_user_id", table_name="user_reminder_settings")
    op.drop_table("user_reminder_settings")
    op.drop_column("users", "reminders_configured")
