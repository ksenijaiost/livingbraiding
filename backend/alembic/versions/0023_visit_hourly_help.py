"""Почасовая помощь в визите (1.7).

Revision ID: 0023_visit_hourly_help
Revises: 0022_visit_correction_master
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_visit_hourly_help"
down_revision = "0022_visit_correction_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("visits", sa.Column("hourly_help_json", sa.Text(), nullable=True))
    op.add_column(
        "visits",
        sa.Column("hourly_help_total", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("visits", "hourly_help_total")
    op.drop_column("visits", "hourly_help_json")
