"""Нормализованный телефон клиента (РФ), уникальный, если номер есть.

Revision ID: 0059_client_phone_normalized
Revises: 0058_notify_channel_prefs
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_client_phone_normalized"
down_revision = "0058_notify_channel_prefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("phone_normalized", sa.String(length=11), nullable=True))
    from app.phone_ru import backfill_client_phone_normalized

    backfill_client_phone_normalized(op.get_bind())
    op.create_index(
        "uq_clients_phone_normalized",
        "clients",
        ["phone_normalized"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_clients_phone_normalized", table_name="clients")
    op.drop_column("clients", "phone_normalized")
