"""Резерв комплекта по ключам: breakdown_json и починка legacy-резервов.

Revision ID: 0037_split_unkeyed_kit_reserves
Revises: 0036_admin_senior_role
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_split_unkeyed_kit_reserves"
down_revision = "0036_admin_senior_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kit_reserves", sa.Column("reserve_breakdown_json", sa.Text(), nullable=True))
    from app.db.session import SessionLocal
    from app.kit_blank_stock_core import repair_all_merged_keyed_kit_reserves, repair_all_unkeyed_kit_reserves

    with SessionLocal() as session:
        repair_all_unkeyed_kit_reserves(session)
        repair_all_merged_keyed_kit_reserves(session)
        session.commit()


def downgrade() -> None:
    op.drop_column("kit_reserves", "reserve_breakdown_json")
