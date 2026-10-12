"""После визита: задержка, «не позже», scheduled_at и visit_id в outbox.

Revision ID: 0053_after_visit_timing
Revises: 0052_client_bot_replies
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_after_visit_timing"
down_revision = "0052_client_bot_replies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("client_notification_rules") as batch:
        batch.alter_column(
            "hours",
            existing_type=sa.Integer(),
            type_=sa.Float(),
            existing_nullable=False,
        )
        batch.add_column(sa.Column("latest_time", sa.Time(), nullable=True))
    with op.batch_alter_table("notification_outbox") as batch:
        batch.add_column(sa.Column("scheduled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("visit_id", sa.Integer(), nullable=True))
    op.create_index("ix_notification_outbox_scheduled_at", "notification_outbox", ["scheduled_at"])
    op.create_index("ix_notification_outbox_visit_id", "notification_outbox", ["visit_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_visit_id", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_scheduled_at", table_name="notification_outbox")
    with op.batch_alter_table("notification_outbox") as batch:
        batch.drop_column("visit_id")
        batch.drop_column("scheduled_at")
    with op.batch_alter_table("client_notification_rules") as batch:
        batch.drop_column("latest_time")
        batch.alter_column(
            "hours",
            existing_type=sa.Float(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
