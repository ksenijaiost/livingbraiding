"""Глобальные правила уведомлений клиентам + маркер списка услуг.

Revision ID: 0048_client_notification_rules
Revises: 0047_client_channel_ids
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_client_notification_rules"
down_revision = "0047_client_channel_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_notification_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("hours", sa.Integer(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_client_notification_rules_kind", "client_notification_rules", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_client_notification_rules_kind", table_name="client_notification_rules")
    op.drop_table("client_notification_rules")
