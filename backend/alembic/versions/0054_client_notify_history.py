"""История уведомлений: входящие сообщения, rule_id и текст в outbox.

Revision ID: 0054_client_notify_history
Revises: 0053_after_visit_timing
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_client_notify_history"
down_revision = "0053_after_visit_timing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_inbound_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("external_user_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("parsed_action", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_client_inbound_messages_client_id", "client_inbound_messages", ["client_id"])
    op.create_index("ix_client_inbound_messages_booking_id", "client_inbound_messages", ["booking_id"])
    with op.batch_alter_table("notification_outbox") as batch:
        batch.add_column(sa.Column("rule_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("body", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_column("body")
        batch.drop_column("rule_id")
    op.drop_index("ix_client_inbound_messages_booking_id", table_name="client_inbound_messages")
    op.drop_index("ix_client_inbound_messages_client_id", table_name="client_inbound_messages")
    op.drop_table("client_inbound_messages")
