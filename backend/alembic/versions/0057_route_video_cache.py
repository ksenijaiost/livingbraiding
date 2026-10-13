"""Кэш вложения видео «Как нас найти» по каналам.

Revision ID: 0057_route_video_cache
Revises: 0056_drop_booking_link
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_route_video_cache"
down_revision = "0056_drop_booking_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("studio_template_variables") as batch:
        batch.add_column(sa.Column("vk_video_attachment", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("max_video_token", sa.Text(), nullable=True))
        batch.add_column(sa.Column("telegram_video_file_id", sa.String(length=256), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("studio_template_variables") as batch:
        batch.drop_column("telegram_video_file_id")
        batch.drop_column("max_video_token")
        batch.drop_column("vk_video_attachment")
