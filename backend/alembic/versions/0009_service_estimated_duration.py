"""Ориентировочное время услуги и часы отображения календаря.

Revision ID: 0009_service_estimated_duration
Revises: 0008_visit_drafts
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_service_estimated_duration"
down_revision = "0008_visit_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "services",
        sa.Column(
            "estimated_duration_minutes",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
    )
    op.execute("UPDATE services SET estimated_duration_minutes = 120")
    op.alter_column("services", "estimated_duration_minutes", server_default=None)


def downgrade() -> None:
    op.drop_column("services", "estimated_duration_minutes")
