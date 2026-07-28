"""История удалений суперадмина (super_admin_purge_logs).

Revision ID: 0028_super_admin_purge_logs
Revises: 0027_payroll_ledger_effective_at
Create Date: 2026-07-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_super_admin_purge_logs"
down_revision = "0027_payroll_ledger_effective_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "super_admin_purge_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purged_at", sa.DateTime(), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("entity_kind", sa.String(length=40), nullable=False),
        sa.Column("entity_ids_text", sa.String(length=500), nullable=False),
        sa.Column("heading", sa.String(length=240), nullable=True),
        sa.Column("details_text", sa.Text(), nullable=True),
    )
    op.create_index("ix_super_admin_purge_logs_purged_at", "super_admin_purge_logs", ["purged_at"])


def downgrade() -> None:
    op.drop_index("ix_super_admin_purge_logs_purged_at", table_name="super_admin_purge_logs")
    op.drop_table("super_admin_purge_logs")
