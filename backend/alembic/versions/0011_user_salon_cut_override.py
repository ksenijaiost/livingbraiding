"""Индивидуальный процент салона у сотрудника.

Revision ID: 0011_user_salon_cut_override
Revises: 0010_master_schedule
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_user_salon_cut_override"
down_revision = "0010_master_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("salon_cut_pct_override", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "salon_cut_pct_override")

