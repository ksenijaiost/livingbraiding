"""Типы уведомлений отдельно для VK, Max и Telegram.

Revision ID: 0058_notify_channel_prefs
Revises: 0057_route_video_cache
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_notify_channel_prefs"
down_revision = "0057_route_video_cache"
branch_labels = None
depends_on = None

_PAIRS = (
    ("bookings", "notify_bookings"),
    ("visit", "notify_visit"),
    ("work", "notify_work"),
    ("hourly_work", "notify_hourly_work"),
    ("product_sale", "notify_product_sale"),
    ("consultation", "notify_consultation"),
    ("work_plan", "notify_work_plan"),
)


def upgrade() -> None:
    op.create_table(
        "user_notify_channel_prefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("pref_key", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("user_id", "channel", "pref_key", name="uq_user_notify_channel_pref"),
    )
    op.create_index("ix_user_notify_channel_prefs_user_id", "user_notify_channel_prefs", ["user_id"])
    from app.notify_prefs import copy_shared_notify_prefs_to_channels

    copy_shared_notify_prefs_to_channels(op.get_bind())
    op.alter_column("user_notify_channel_prefs", "enabled", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_user_notify_channel_prefs_user_id", table_name="user_notify_channel_prefs")
    op.drop_table("user_notify_channel_prefs")
