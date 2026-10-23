"""Два общих чата студии и длинные настройки внутренних уведомлений.

Revision ID: 0067_studio_chats_and_internal_notify
Revises: 0066_notify_booking_nullable
Create Date: 2026-10-23

Существующие строки admin_chat_targets и токены получают chat_kind='admin'.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_studio_chats_and_internal_notify"
down_revision = "0066_notify_booking_nullable"
branch_labels = None
depends_on = None

_OUTBOX_TARGET_CK = (
    "(target_kind = 'user' AND user_id IS NOT NULL AND client_id IS NULL) OR "
    "(target_kind = 'client' AND client_id IS NOT NULL AND user_id IS NULL) OR "
    "(target_kind = 'admin_chat' AND user_id IS NULL) OR "
    "(target_kind = 'general_chat' AND user_id IS NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("admin_chat_targets") as batch:
        batch.add_column(
            sa.Column("chat_kind", sa.String(length=16), nullable=False, server_default="admin")
        )
        batch.drop_constraint("uq_admin_chat_targets_channel", type_="unique")
        batch.create_unique_constraint(
            "uq_admin_chat_targets_kind_channel", ["chat_kind", "channel"]
        )
    with op.batch_alter_table("admin_chat_link_tokens") as batch:
        batch.add_column(
            sa.Column("chat_kind", sa.String(length=16), nullable=False, server_default="admin")
        )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_notification_outbox_target", type_="check")
        batch.create_check_constraint("ck_notification_outbox_target", _OUTBOX_TARGET_CK)
    with op.batch_alter_table("settings") as batch:
        batch.alter_column(
            "value",
            existing_type=sa.String(length=500),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("settings") as batch:
        batch.alter_column(
            "value",
            existing_type=sa.Text(),
            type_=sa.String(length=500),
            existing_nullable=False,
        )
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_notification_outbox_target", type_="check")
        batch.create_check_constraint(
            "ck_notification_outbox_target",
            "(target_kind = 'user' AND user_id IS NOT NULL AND client_id IS NULL) OR "
            "(target_kind = 'client' AND client_id IS NOT NULL AND user_id IS NULL) OR "
            "(target_kind = 'admin_chat' AND user_id IS NULL AND client_id IS NULL)",
        )
    with op.batch_alter_table("admin_chat_link_tokens") as batch:
        batch.drop_column("chat_kind")
    with op.batch_alter_table("admin_chat_targets") as batch:
        batch.drop_constraint("uq_admin_chat_targets_kind_channel", type_="unique")
        batch.create_unique_constraint("uq_admin_chat_targets_channel", ["channel"])
        batch.drop_column("chat_kind")
