"""Удалить kits.has_decorations и kits.blanks_kinds_text (заменено примечаниями / типами заготовок).

Revision ID: 0005_drop_kit_decor_blanks_kinds
Revises: 0004_kits_blanks_condition
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

revision = "0005_drop_kit_decor_blanks_kinds"
down_revision = "0004_kits_blanks_condition"
branch_labels = None
depends_on = None


def _kits_columns(connection) -> set[str]:
    insp = inspect(connection)
    return {c["name"] for c in insp.get_columns("kits")}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _kits_columns(conn)
    if "has_decorations" in cols:
        op.drop_column("kits", "has_decorations")
    if "blanks_kinds_text" in cols:
        op.drop_column("kits", "blanks_kinds_text")


def downgrade() -> None:
    import sqlalchemy as sa

    conn = op.get_bind()
    cols = _kits_columns(conn)
    if "has_decorations" not in cols:
        op.add_column(
            "kits",
            sa.Column("has_decorations", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
        op.alter_column("kits", "has_decorations", server_default=None)
    if "blanks_kinds_text" not in cols:
        op.add_column("kits", sa.Column("blanks_kinds_text", sa.Text(), nullable=True))
