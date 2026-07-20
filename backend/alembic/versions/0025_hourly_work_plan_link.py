"""Связь почасовой работы с планом работ.

Revision ID: 0025_hourly_work_plan_link
Revises: 0024_hourly_work
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_hourly_work_plan_link"
down_revision = "0024_hourly_work"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hourly_work_entries",
        sa.Column("work_plan_id", sa.Integer(), sa.ForeignKey("work_plans.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("hourly_work_entries", "work_plan_id")
