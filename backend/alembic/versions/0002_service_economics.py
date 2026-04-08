"""add service economics fields

Revision ID: 0002_service_economics
Revises: 0001_init
Create Date: 2026-04-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_service_economics"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("services", sa.Column("master_pay_amount", sa.Float(), nullable=True))
    op.add_column("services", sa.Column("studio_pay_amount", sa.Float(), nullable=True))
    op.add_column("services", sa.Column("fixed_expense_amount", sa.Float(), nullable=True))
    op.add_column("services", sa.Column("is_per_unit", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("services", sa.Column("unit_label", sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column("services", "unit_label")
    op.drop_column("services", "is_per_unit")
    op.drop_column("services", "fixed_expense_amount")
    op.drop_column("services", "studio_pay_amount")
    op.drop_column("services", "master_pay_amount")

