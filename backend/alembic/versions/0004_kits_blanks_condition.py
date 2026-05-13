"""kits.blanks_condition + product_sales.kit_lines_json (несколько комплектов в продаже).

Revision ID: 0004_kits_blanks_condition
Revises: 0003_visits_kit_paid_fix
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0004_kits_blanks_condition"
down_revision = "0003_visits_kit_paid_fix"
branch_labels = None
depends_on = None


def _kits_column_names(connection) -> set[str]:
    insp = inspect(connection)
    return {c["name"] for c in insp.get_columns("kits")}


def _product_sales_column_names(connection) -> set[str]:
    insp = inspect(connection)
    return {c["name"] for c in insp.get_columns("product_sales")}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _kits_column_names(conn)
    if "blanks_condition" not in cols:
        op.add_column(
            "kits",
            sa.Column(
                "blanks_condition",
                sa.String(length=16),
                nullable=False,
                server_default="NEW",
            ),
        )
        op.alter_column("kits", "blanks_condition", server_default=None)

    pcols = _product_sales_column_names(conn)
    if "kit_lines_json" not in pcols:
        op.add_column(
            "product_sales",
            sa.Column("kit_lines_json", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    pcols = _product_sales_column_names(conn)
    if "kit_lines_json" in pcols:
        op.drop_column("product_sales", "kit_lines_json")

    cols = _kits_column_names(conn)
    if "blanks_condition" in cols:
        op.drop_column("kits", "blanks_condition")
