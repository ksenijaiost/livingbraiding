"""Клиентские уведомления: outbox client_id, подтверждение брони, чат админов, sends.

Revision ID: 0049_client_notify_confirm
Revises: 0048_client_notification_rules
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_client_notify_confirm"
down_revision = "0048_client_notification_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True))
        batch.add_column(
            sa.Column("target_kind", sa.String(length=16), nullable=False, server_default="user")
        )
        batch.create_index("ix_notification_outbox_client_id", ["client_id"])
        batch.create_check_constraint(
            "ck_notification_outbox_target",
            "(target_kind = 'user' AND user_id IS NOT NULL AND client_id IS NULL) OR "
            "(target_kind = 'client' AND client_id IS NOT NULL AND user_id IS NULL) OR "
            "(target_kind = 'admin_chat' AND user_id IS NULL AND client_id IS NULL)",
        )

    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("client_confirmed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("client_confirmed_via", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("client_cancel_requested_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("client_cancel_requested_via", sa.String(length=16), nullable=True))

    with op.batch_alter_table("telegram_link_tokens") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(
            sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=True)
        )
        batch.create_index("ix_telegram_link_tokens_client_id", ["client_id"])
        batch.create_check_constraint(
            "ck_telegram_link_tokens_subject",
            "(user_id IS NOT NULL AND client_id IS NULL) OR (user_id IS NULL AND client_id IS NOT NULL)",
        )

    op.create_table(
        "client_notification_sends",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("client_notification_rules.id"), nullable=True),
        sa.Column("outbox_id", sa.Integer(), sa.ForeignKey("notification_outbox.id"), nullable=True),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_client_notification_sends_client_id", "client_notification_sends", ["client_id"])
    op.create_index("ix_client_notification_sends_booking_id", "client_notification_sends", ["booking_id"])
    op.create_index(
        "ix_client_notification_sends_lookup",
        "client_notification_sends",
        ["client_id", "event_type", "sent_at"],
    )

    op.create_table(
        "admin_chat_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("channel", name="uq_admin_chat_targets_channel"),
    )

    op.create_table(
        "admin_chat_link_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_admin_chat_link_tokens_hash"),
    )


def downgrade() -> None:
    op.drop_table("admin_chat_link_tokens")
    op.drop_table("admin_chat_targets")
    op.drop_index("ix_client_notification_sends_lookup", table_name="client_notification_sends")
    op.drop_index("ix_client_notification_sends_booking_id", table_name="client_notification_sends")
    op.drop_index("ix_client_notification_sends_client_id", table_name="client_notification_sends")
    op.drop_table("client_notification_sends")

    with op.batch_alter_table("telegram_link_tokens") as batch:
        batch.drop_constraint("ck_telegram_link_tokens_subject", type_="check")
        batch.drop_index("ix_telegram_link_tokens_client_id")
        batch.drop_column("client_id")
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("client_cancel_requested_via")
        batch.drop_column("client_cancel_requested_at")
        batch.drop_column("client_confirmed_via")
        batch.drop_column("client_confirmed_at")

    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_constraint("ck_notification_outbox_target", type_="check")
        batch.drop_index("ix_notification_outbox_client_id")
        batch.drop_column("target_kind")
        batch.drop_column("client_id")
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
