"""Черновики работы с товарами.

Revision ID: 0031_work_drafts
Revises: 0030_hourly_work_void
Create Date: 2026-08-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_work_drafts"
down_revision = "0030_hourly_work_void"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("performed_date", sa.DateTime(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("work_plan_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=True),
        sa.Column("scope", sa.String(length=24), nullable=True),
        sa.Column("form_json", sa.Text(), nullable=True),
        sa.Column("preview_json", sa.Text(), nullable=True),
        sa.Column("locked_by_user_id", sa.Integer(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("finalized_work_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["finalized_work_id"], ["work_for_inventory.id"]),
        sa.ForeignKeyConstraint(["locked_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["work_plan_id"], ["work_plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_drafts_performed_date", "work_drafts", ["performed_date"], unique=False)
    op.create_index("ix_work_drafts_finalized_work_id", "work_drafts", ["finalized_work_id"], unique=False)

    op.create_table(
        "work_draft_participants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_draft_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["master_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["work_draft_id"], ["work_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_draft_id", "master_id", name="uq_work_draft_participant"),
    )


def downgrade() -> None:
    op.drop_table("work_draft_participants")
    op.drop_index("ix_work_drafts_finalized_work_id", table_name="work_drafts")
    op.drop_index("ix_work_drafts_performed_date", table_name="work_drafts")
    op.drop_table("work_drafts")
