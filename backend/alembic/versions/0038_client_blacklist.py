"""Add client blacklist fields.

Revision ID: 0038_client_blacklist
Revises: 0037_split_unkeyed_kit_reserves
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_client_blacklist"
down_revision = "0037_split_unkeyed_kit_reserves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("is_blacklisted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("clients", sa.Column("blacklist_comment", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "blacklist_comment")
    op.drop_column("clients", "is_blacklisted")
