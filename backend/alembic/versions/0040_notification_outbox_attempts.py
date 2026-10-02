"""attempt_count в notification_outbox для лимита ретраев.

Revision ID: 0040_notification_outbox_attempts
Revises: 0039_notification_outbox
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_notification_outbox_attempts"
down_revision = "0039_notification_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("notification_outbox", "attempt_count")
