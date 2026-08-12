"""Автор создания комплекта (kits.created_by_user_id).

Revision ID: 0033_kit_created_by_user
Revises: 0032_helper_role
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_kit_created_by_user"
down_revision = "0032_helper_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("kits", sa.Column("created_by_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_kits_created_by_user_id_users",
        "kits",
        "users",
        ["created_by_user_id"],
        ["id"],
    )
    op.execute(
        """
        UPDATE kits
        SET created_by_user_id = updated_by_user_id
        WHERE created_by_user_id IS NULL AND updated_by_user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_kits_created_by_user_id_users", "kits", type_="foreignkey")
    op.drop_column("kits", "created_by_user_id")
