"""kits.blanks_condition: новый / Б/У / смешанный набор.

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


def upgrade() -> None:
    conn = op.get_bind()
    cols = _kits_column_names(conn)
    if "blanks_condition" in cols:
        return
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


def downgrade() -> None:
    conn = op.get_bind()
    cols = _kits_column_names(conn)
    if "blanks_condition" not in cols:
        return
    op.drop_column("kits", "blanks_condition")
