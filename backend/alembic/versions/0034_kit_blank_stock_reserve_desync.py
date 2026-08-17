"""1.55: починить рассинхрон pieces_available / kit_blank_stock после резерва «на заказ».

Revision ID: 0034_kit_blank_stock_desync
Revises: 0033_kit_created_by_user
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op

revision = "0034_kit_blank_stock_desync"
down_revision = "0033_kit_created_by_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.orm import Session

    from app.kit_blank_stock_core import repair_all_kits_blank_stock_reserve_desync

    session = Session(bind=op.get_bind())
    try:
        repair_all_kits_blank_stock_reserve_desync(session)
        session.flush()
    finally:
        session.close()


def downgrade() -> None:
    pass
