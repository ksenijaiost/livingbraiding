"""Правила уведомлений клиенту при смене статуса брони.

Revision ID: 0060_booking_status_notify
Revises: 0059_client_phone_normalized
Create Date: 2026-10-19
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0060_booking_status_notify"
down_revision = "0059_client_phone_normalized"
branch_labels = None
depends_on = None

_CONFIRMED = (
    "Здравствуйте, {{name}}! Ваша запись подтверждена: {{date_text}} в {{time}}, "
    "{{services}}. Ждём вас по адресу: {{address}}"
)


def upgrade() -> None:
    op.create_table(
        "booking_status_notify_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("attach_memo_before", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("status", name="uq_booking_status_notify_rules_status"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO booking_status_notify_rules
                (status, template, attach_memo_before, is_enabled, position, created_at)
            SELECT :status, :template, :memo, :enabled, 0, :created_at
            WHERE NOT EXISTS (
                SELECT 1 FROM booking_status_notify_rules WHERE status = :status
            )
            """
        ).bindparams(
            status="ACTIVE",
            template=_CONFIRMED,
            memo=True,
            enabled=True,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )


def downgrade() -> None:
    op.drop_table("booking_status_notify_rules")
