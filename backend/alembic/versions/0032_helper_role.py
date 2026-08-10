"""Роль HELPER (помощник).

Revision ID: 0032_helper_role
Revises: 0031_work_drafts
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op

revision = "0032_helper_role"
down_revision = "0031_work_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'HELPER'")


def downgrade() -> None:
    # Удаление значения из PG ENUM без пересоздания типа не поддерживается.
    pass
