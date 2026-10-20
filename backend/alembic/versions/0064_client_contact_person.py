"""Контактное лицо клиента и pending выбора брони в мессенджере.

Revision ID: 0064_client_contact_person
Revises: 0063_booking_discount_dialog
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_client_contact_person"
down_revision = "0063_booking_discount_dialog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("clients") as batch:
        batch.add_column(sa.Column("contact_client_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("contact_relation", sa.String(length=50), nullable=True))
        batch.create_foreign_key(
            "fk_clients_contact_client_id",
            "clients",
            ["contact_client_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_clients_contact_client_id", ["contact_client_id"])

    op.create_table(
        "client_messenger_pending",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("messenger_user_id", sa.BigInteger(), nullable=False),
        # await_booking_choice | selected_booking
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_client_messenger_pending_channel_user",
        "client_messenger_pending",
        ["channel", "messenger_user_id"],
    )
    op.create_index("ix_client_messenger_pending_expires_at", "client_messenger_pending", ["expires_at"])
    op.create_index("ix_client_messenger_pending_client_id", "client_messenger_pending", ["client_id"])


def downgrade() -> None:
    op.drop_table("client_messenger_pending")
    with op.batch_alter_table("clients") as batch:
        batch.drop_index("ix_clients_contact_client_id")
        batch.drop_constraint("fk_clients_contact_client_id", type_="foreignkey")
        batch.drop_column("contact_relation")
        batch.drop_column("contact_client_id")
