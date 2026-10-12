"""Памятки после визита и привязка в каталоге.

Revision ID: 0055_care_memos
Revises: 0054_client_notify_history
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_care_memos"
down_revision = "0054_client_notify_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "care_memos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("file_path", sa.String(length=255), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("mime", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("vk_attachment", sa.String(length=80), nullable=True),
        sa.Column("max_image_token", sa.Text(), nullable=True),
        sa.Column("telegram_file_id", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("name", name="uq_care_memos_name"),
    )
    with op.batch_alter_table("service_categories") as batch:
        batch.add_column(sa.Column("care_memo_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("service_subcategories") as batch:
        batch.add_column(
            sa.Column("care_memo_mode", sa.String(length=16), nullable=False, server_default="inherit")
        )
        batch.add_column(sa.Column("care_memo_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("services") as batch:
        batch.add_column(
            sa.Column("care_memo_mode", sa.String(length=16), nullable=False, server_default="inherit")
        )
        batch.add_column(sa.Column("care_memo_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("client_notification_rules") as batch:
        batch.add_column(
            sa.Column("send_care_memo", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("client_notification_rules") as batch:
        batch.drop_column("send_care_memo")
    with op.batch_alter_table("services") as batch:
        batch.drop_column("care_memo_id")
        batch.drop_column("care_memo_mode")
    with op.batch_alter_table("service_subcategories") as batch:
        batch.drop_column("care_memo_id")
        batch.drop_column("care_memo_mode")
    with op.batch_alter_table("service_categories") as batch:
        batch.drop_column("care_memo_id")
    op.drop_table("care_memos")
