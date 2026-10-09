"""Отметки клиента и отписка от акций.

Revision ID: 0050_client_marks
Revises: 0049_client_notify_confirm
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_client_marks"
down_revision = "0049_client_notify_confirm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("no_promo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("clients", sa.Column("no_promo_at", sa.DateTime(), nullable=True))
    op.add_column("clients", sa.Column("no_promo_source", sa.String(length=20), nullable=True))
    op.add_column(
        "clients",
        sa.Column("interested_in_training", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "clients",
        sa.Column("is_student", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "clients",
        sa.Column("is_model", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("clients", "is_model")
    op.drop_column("clients", "is_student")
    op.drop_column("clients", "interested_in_training")
    op.drop_column("clients", "no_promo_source")
    op.drop_column("clients", "no_promo_at")
    op.drop_column("clients", "no_promo")
