"""batch_id и source в master_schedule_audit_logs для массовых операций графика.

Revision ID: 0069_master_schedule_audit_batch
Revises: 0068_amortization_levels
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_master_schedule_audit_batch"
down_revision = "0068_amortization_levels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "master_schedule_audit_logs",
        sa.Column("batch_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "master_schedule_audit_logs",
        sa.Column("source", sa.String(length=32), nullable=False, server_default="single"),
    )
    op.create_index(
        "ix_master_schedule_audit_logs_batch_id",
        "master_schedule_audit_logs",
        ["batch_id"],
    )
    op.create_index(
        "ix_master_schedule_audit_logs_master_changed",
        "master_schedule_audit_logs",
        ["master_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_master_schedule_audit_logs_master_changed", table_name="master_schedule_audit_logs")
    op.drop_index("ix_master_schedule_audit_logs_batch_id", table_name="master_schedule_audit_logs")
    op.drop_column("master_schedule_audit_logs", "source")
    op.drop_column("master_schedule_audit_logs", "batch_id")
