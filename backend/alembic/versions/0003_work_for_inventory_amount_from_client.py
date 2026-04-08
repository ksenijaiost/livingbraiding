"""add amount_from_client to work_for_inventory

Revision ID: 0003_work_for_inventory_amount_from_client
Revises: 0002_service_economics
Create Date: 2026-04-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_work_for_inventory_amount_from_client"
down_revision = "0002_service_economics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_for_inventory", sa.Column("amount_from_client", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_for_inventory", "amount_from_client")

