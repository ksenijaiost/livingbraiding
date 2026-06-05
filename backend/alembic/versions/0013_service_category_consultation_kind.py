"""Вид консультации у категории прайса.

Revision ID: 0013_service_category_consultation_kind
Revises: 0012_booking_planned_service_comment
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_service_category_consultation_kind"
down_revision = "0012_booking_planned_service_comment"
branch_labels = None
depends_on = None

_CONSULTATION_KIND = sa.Enum("BRAIDING", "EXTENSION", "OTHER", name="consultationkind")

_EXTENSION_NAMES = frozenset({"Наращивание"})
_OTHER_NAMES = frozenset({"Снятие", "Уход", "Обучение"})


def upgrade() -> None:
    _CONSULTATION_KIND.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "service_categories",
        sa.Column(
            "consultation_kind",
            _CONSULTATION_KIND,
            nullable=False,
            server_default="BRAIDING",
        ),
    )

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, name FROM service_categories")).fetchall()
    for row in rows:
        name = str(row.name or "").strip()
        if name in _EXTENSION_NAMES:
            kind = "EXTENSION"
        elif name in _OTHER_NAMES:
            kind = "OTHER"
        else:
            kind = "BRAIDING"
        conn.execute(
            sa.text("UPDATE service_categories SET consultation_kind = :kind WHERE id = :id"),
            {"kind": kind, "id": row.id},
        )

    op.alter_column("service_categories", "consultation_kind", server_default=None)


def downgrade() -> None:
    op.drop_column("service_categories", "consultation_kind")
    _CONSULTATION_KIND.drop(op.get_bind(), checkfirst=True)
