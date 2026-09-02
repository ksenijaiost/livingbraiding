"""Один резерв — несколько ключей: reserve_breakdown_json.

Revision ID: 0038_kit_reserve_breakdown_json
Revises: 0037_split_unkeyed_kit_reserves
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0038_kit_reserve_breakdown_json"
down_revision = "0037_split_unkeyed_kit_reserves"
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
