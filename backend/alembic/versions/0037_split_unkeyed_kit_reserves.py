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
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "ALTER TABLE kit_reserves "
                "ADD COLUMN IF NOT EXISTS reserve_breakdown_json TEXT"
            )
        )
    else:
        op.add_column("kit_reserves", sa.Column("reserve_breakdown_json", sa.Text(), nullable=True))

    from sqlalchemy.orm import Session

    from app.kit_blank_stock_core import repair_all_merged_keyed_kit_reserves, repair_all_unkeyed_kit_reserves

    # Как в 0034/0035: одна транзакция Alembic, тот же bind. SessionLocal() + commit()
    # открывает второе соединение к kit_reserves и на PostgreSQL зависает на DDL-lock.
    session = Session(bind=bind)
    try:
        repair_all_unkeyed_kit_reserves(session)
        repair_all_merged_keyed_kit_reserves(session)
        session.flush()
    finally:
        session.close()


def downgrade() -> None:
    op.drop_column("kit_reserves", "reserve_breakdown_json")
