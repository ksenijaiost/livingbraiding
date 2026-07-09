"""Планы работ (Fix: план работ).

Revision ID: 0021_work_plans
Revises: 0020_catalog_short_names
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_work_plans"
down_revision = "0020_catalog_short_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("planned_date", sa.DateTime(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("master_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("plan_type", sa.String(length=24), nullable=False, server_default="WORK_PRODUCT"),
        sa.Column("work_kind", sa.String(length=32), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="PLANNED"),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_work_plans_planned_date", "work_plans", ["planned_date"])
    op.create_index("ix_work_plans_master_id", "work_plans", ["master_id"])
    op.create_index("ix_work_plans_status", "work_plans", ["status"])
    op.add_column(
        "work_for_inventory",
        sa.Column("work_plan_id", sa.Integer(), sa.ForeignKey("work_plans.id"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("work_for_inventory", "work_plan_id")
    op.drop_index("ix_work_plans_status", table_name="work_plans")
    op.drop_index("ix_work_plans_master_id", table_name="work_plans")
    op.drop_index("ix_work_plans_planned_date", table_name="work_plans")
    op.drop_table("work_plans")
