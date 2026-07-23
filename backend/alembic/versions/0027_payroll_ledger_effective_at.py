"""Учётная дата проводки фонда ЗП (effective_at).

Revision ID: 0027_payroll_ledger_effective_at
Revises: 0026_product_sale_kit_other_cost
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_payroll_ledger_effective_at"
down_revision = "0026_product_sale_kit_other_cost"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payroll_fund_ledger", sa.Column("effective_at", sa.DateTime(), nullable=True))

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE payroll_fund_ledger SET effective_at = created_at WHERE effective_at IS NULL"
        )
    )

    # Python backfill по датам событий (уважает PAYROLL_LEDGER_BACKFILL_CLOSED).
    from sqlalchemy.orm import sessionmaker

    from app.payroll_ledger_backfill import backfill_payroll_ledger_effective_at

    SessionLocal = sessionmaker(bind=bind)
    db = SessionLocal()
    try:
        backfill_payroll_ledger_effective_at(db)
        db.commit()
    finally:
        db.close()

    op.alter_column("payroll_fund_ledger", "effective_at", existing_type=sa.DateTime(), nullable=False)
    op.create_index("ix_payroll_fund_ledger_effective_at", "payroll_fund_ledger", ["effective_at"])


def downgrade() -> None:
    op.drop_index("ix_payroll_fund_ledger_effective_at", table_name="payroll_fund_ledger")
    op.drop_column("payroll_fund_ledger", "effective_at")
