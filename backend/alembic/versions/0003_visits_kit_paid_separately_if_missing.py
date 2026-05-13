"""Добавить visits.kit_paid_separately, если колонки нет (восстановление после частичного применения 0002).

Revision ID: 0003_visits_kit_paid_fix
Revises: 0002_kit_reserve_booking
Create Date: 2026-05-13

На проде встречалось: alembic_version уже 0002, но колонка не создана — приложение падает при SELECT.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0003_visits_kit_paid_fix"
down_revision = "0002_kit_reserve_booking"
branch_labels = None
depends_on = None


def _visits_column_names(connection) -> set[str]:
    insp = inspect(connection)
    return {c["name"] for c in insp.get_columns("visits")}


def upgrade() -> None:
    conn = op.get_bind()
    cols = _visits_column_names(conn)
    if "kit_paid_separately" in cols:
        return
    op.add_column(
        "visits",
        sa.Column("kit_paid_separately", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.alter_column("visits", "kit_paid_separately", server_default=None)


def downgrade() -> None:
    """Не удаляем колонку: она могла быть создана ревизией 0002; эта миграция только «долечивает» схему."""
    pass
