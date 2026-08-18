"""1.56: выровнять pieces_available по kit_blank_stock после списания без autoflush.

Revision ID: 0035_kit_avail_blank_stock
Revises: 0034_kit_blank_stock_desync
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op

revision = "0035_kit_avail_blank_stock"
down_revision = "0034_kit_blank_stock_desync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy.orm import Session

    from app.kit_blank_stock_core import repair_all_kits_pieces_available_from_blank_stock

    session = Session(bind=op.get_bind())
    try:
        repair_all_kits_pieces_available_from_blank_stock(session)
        session.flush()
    finally:
        session.close()


def downgrade() -> None:
    pass
