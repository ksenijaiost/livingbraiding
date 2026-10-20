"""Настраиваемые цифры ответов клиентки и {{reply_hint}}.

Revision ID: 0061_client_reply_digits
Revises: 0060_booking_status_notify
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_client_reply_digits"
down_revision = "0060_booking_status_notify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_reply_digit_actions",
        sa.Column("action", sa.String(length=32), primary_key=True),
        sa.Column("digit", sa.String(length=1), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("hint_label", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    from app.client_reply_digits import migrate_replace_legacy_reply_hint, seed_reply_digit_actions

    bind = op.get_bind()
    seed_reply_digit_actions(bind)
    migrate_replace_legacy_reply_hint(bind)


def downgrade() -> None:
    op.drop_table("client_reply_digit_actions")
