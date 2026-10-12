"""users.max_user_id для канала Max.

Revision ID: 0046_user_max_user_id
Revises: 0045_user_notify_type_prefs
Create Date: 2026-10-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_user_max_user_id"
down_revision = "0045_user_notify_type_prefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("max_user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_users_max_user_id", "users", ["max_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_max_user_id", table_name="users")
    op.drop_column("users", "max_user_id")
