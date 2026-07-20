"""Мастер коррекции в визите (1.5).

Revision ID: 0022_visit_correction_master
Revises: 0021_work_plans
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_visit_correction_master"
down_revision = "0021_work_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "visits",
        sa.Column("correction_master_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "visits",
        sa.Column("correction_master_amount", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "visit_services",
        sa.Column("correction_master_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "visit_services",
        sa.Column("correction_master_amount", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("visit_services", "correction_master_amount")
    op.drop_column("visit_services", "correction_master_id")
    op.drop_column("visits", "correction_master_amount")
    op.drop_column("visits", "correction_master_id")
