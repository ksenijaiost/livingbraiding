"""clients: telegram_chat_id / vk_user_id / max_user_id для каналов уведомлений.

Revision ID: 0047_client_channel_ids
Revises: 0046_user_max_user_id
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_client_channel_ids"
down_revision = "0046_user_max_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("clients", sa.Column("vk_user_id", sa.BigInteger(), nullable=True))
    op.add_column("clients", sa.Column("max_user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_clients_telegram_chat_id", "clients", ["telegram_chat_id"], unique=True)
    op.create_index("ix_clients_vk_user_id", "clients", ["vk_user_id"], unique=True)
    op.create_index("ix_clients_max_user_id", "clients", ["max_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_clients_max_user_id", table_name="clients")
    op.drop_index("ix_clients_vk_user_id", table_name="clients")
    op.drop_index("ix_clients_telegram_chat_id", table_name="clients")
    op.drop_column("clients", "max_user_id")
    op.drop_column("clients", "vk_user_id")
    op.drop_column("clients", "telegram_chat_id")
