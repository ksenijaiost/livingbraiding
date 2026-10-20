"""Два слота памятки: «до» и «после» в каталоге.

Revision ID: 0062_care_memo_before_after
Revises: 0061_client_reply_digits
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062_care_memo_before_after"
down_revision = "0061_client_reply_digits"
branch_labels = None
depends_on = None

_NEW_COLS = (
    ("care_memo_before_mode", sa.Column("care_memo_before_mode", sa.String(length=16), nullable=False, server_default="inherit")),
    ("care_memo_before_id", sa.Column("care_memo_before_id", sa.Integer(), nullable=True)),
    ("care_memo_after_mode", sa.Column("care_memo_after_mode", sa.String(length=16), nullable=False, server_default="inherit")),
    ("care_memo_after_id", sa.Column("care_memo_after_id", sa.Integer(), nullable=True)),
)


def _add_slot_columns(table: str) -> None:
    with op.batch_alter_table(table) as batch:
        for _name, col in _NEW_COLS:
            batch.add_column(col)


def _drop_slot_columns(table: str) -> None:
    with op.batch_alter_table(table) as batch:
        batch.drop_column("care_memo_after_id")
        batch.drop_column("care_memo_after_mode")
        batch.drop_column("care_memo_before_id")
        batch.drop_column("care_memo_before_mode")


def upgrade() -> None:
    for table in ("service_categories", "service_subcategories", "services"):
        _add_slot_columns(table)

    from app.care_memos import migrate_care_memo_before_after

    migrate_care_memo_before_after(op.get_bind())

    with op.batch_alter_table("service_categories") as batch:
        batch.drop_column("care_memo_id")
    with op.batch_alter_table("service_subcategories") as batch:
        batch.drop_column("care_memo_id")
        batch.drop_column("care_memo_mode")
    with op.batch_alter_table("services") as batch:
        batch.drop_column("care_memo_id")
        batch.drop_column("care_memo_mode")


def downgrade() -> None:
    with op.batch_alter_table("service_categories") as batch:
        batch.add_column(sa.Column("care_memo_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("service_subcategories") as batch:
        batch.add_column(
            sa.Column("care_memo_mode", sa.String(length=16), nullable=False, server_default="inherit")
        )
        batch.add_column(sa.Column("care_memo_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("services") as batch:
        batch.add_column(
            sa.Column("care_memo_mode", sa.String(length=16), nullable=False, server_default="inherit")
        )
        batch.add_column(sa.Column("care_memo_id", sa.Integer(), nullable=True))

    from app.care_memos import downgrade_care_memo_before_after

    downgrade_care_memo_before_after(op.get_bind())

    for table in ("service_categories", "service_subcategories", "services"):
        _drop_slot_columns(table)
