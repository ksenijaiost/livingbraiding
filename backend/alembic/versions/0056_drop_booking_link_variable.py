"""Удалить встроенную переменную booking_link.

Revision ID: 0056_drop_booking_link
Revises: 0055_care_memos
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_drop_booking_link"
down_revision = "0055_care_memos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("DELETE FROM studio_template_variables WHERE key = 'booking_link'"))


def downgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO studio_template_variables (key, value, kind, is_builtin) "
            "SELECT 'booking_link', NULL, 'url', 1 "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM studio_template_variables WHERE key = 'booking_link'"
            ")"
        )
    )
