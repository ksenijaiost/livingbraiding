"""Индексы подсказок комплектов: резерв по клиенту и комплекту.

Revision ID: 0065_kit_suggest_indexes
Revises: 0064_client_contact_person
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_kit_suggest_indexes"
down_revision = "0064_client_contact_person"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_kit_reserves_kit_id", "kit_reserves", ["kit_id"])
    op.create_index(
        "ix_kit_reserves_client_kit",
        "kit_reserves",
        ["reserved_for_client_id", "kit_id"],
    )
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "ix_kits_in_stock_sku",
            "kits",
            ["sku"],
            postgresql_where=sa.text(
                "is_active IS TRUE AND is_archived IS FALSE AND pieces_available > 0"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("ix_kits_in_stock_sku", table_name="kits")
    op.drop_index("ix_kit_reserves_client_kit", table_name="kit_reserves")
    op.drop_index("ix_kit_reserves_kit_id", table_name="kit_reserves")
