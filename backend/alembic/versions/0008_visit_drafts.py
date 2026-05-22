"""Черновики визита.

Revision ID: 0008_visit_drafts
Revises: 0007_booking_pending_confirmation
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_visit_drafts"
down_revision = "0007_booking_pending_confirmation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "visit_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("performed_date", sa.DateTime(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("form_json", sa.Text(), nullable=True),
        sa.Column("preview_json", sa.Text(), nullable=True),
        sa.Column("locked_by_user_id", sa.Integer(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("finalized_visit_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["finalized_visit_id"], ["visits.id"]),
        sa.ForeignKeyConstraint(["locked_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_visit_drafts_performed_date", "visit_drafts", ["performed_date"], unique=False)
    op.create_index("ix_visit_drafts_finalized_visit_id", "visit_drafts", ["finalized_visit_id"], unique=False)

    op.create_table(
        "visit_draft_participants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("visit_draft_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["master_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["visit_draft_id"], ["visit_drafts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("visit_draft_id", "master_id", name="uq_visit_draft_participant"),
    )


def downgrade() -> None:
    op.drop_table("visit_draft_participants")
    op.drop_index("ix_visit_drafts_finalized_visit_id", table_name="visit_drafts")
    op.drop_index("ix_visit_drafts_performed_date", table_name="visit_drafts")
    op.drop_table("visit_drafts")
