"""Нал/безнал для суммы с клиента (Fix 100).

Revision ID: 0019_client_payment_kind
Revises: 0018_consultation_source_booking
Create Date: 2026-07-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_client_payment_kind"
down_revision = "0018_consultation_source_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "visit_services",
        sa.Column("client_payment_kind", sa.String(length=16), nullable=False, server_default="CASH"),
    )
    op.add_column(
        "product_sales",
        sa.Column("client_payment_kind", sa.String(length=16), nullable=False, server_default="CASH"),
    )
    op.add_column(
        "work_for_inventory",
        sa.Column("client_payment_kind", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("work_for_inventory", "client_payment_kind")
    op.drop_column("product_sales", "client_payment_kind")
    op.drop_column("visit_services", "client_payment_kind")
