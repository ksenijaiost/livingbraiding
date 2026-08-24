"""Роль ADMIN_SENIOR (старший админ).

Revision ID: 0036_admin_senior_role
Revises: 0035_kit_avail_blank_stock
Create Date: 2026-08-24
"""

from __future__ import annotations

from alembic import op

revision = "0036_admin_senior_role"
down_revision = "0035_kit_avail_blank_stock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'ADMIN_SENIOR'")


def downgrade() -> None:
    pass
