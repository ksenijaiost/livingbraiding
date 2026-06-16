"""Ручная занятость мастера (блоки времени с комментарием).

Revision ID: 0016_master_time_blocks
Revises: 0015_booking_planned_service_duration
Create Date: 2026-06-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_master_time_blocks"
down_revision = "0015_booking_planned_service_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "master_time_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("block_date", sa.Date(), nullable=False),
        sa.Column("time_from", sa.Time(), nullable=False),
        sa.Column("time_to", sa.Time(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["master_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_master_time_blocks_master_date", "master_time_blocks", ["master_id", "block_date"])


def downgrade() -> None:
    op.drop_index("ix_master_time_blocks_master_date", table_name="master_time_blocks")
    op.drop_table("master_time_blocks")
