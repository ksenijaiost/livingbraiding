"""Переменные студии для шаблонов уведомлений.

Revision ID: 0051_studio_template_variables
Revises: 0050_client_marks
Create Date: 2026-10-12
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0051_studio_template_variables"
down_revision = "0050_client_marks"
branch_labels = None
depends_on = None

_BUILTINS = (
    ("address", "text"),
    ("route_video", "video"),
    ("review_yandex", "url"),
    ("review_2gis", "url"),
    ("booking_link", "url"),
    ("studio_vk", "url"),
    ("studio_telegram", "url"),
    ("studio_max", "url"),
)


def upgrade() -> None:
    op.create_table(
        "studio_template_variables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("file_path", sa.String(length=300), nullable=True),
        sa.Column("file_name", sa.String(length=200), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("public_token", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("key", name="uq_studio_template_variables_key"),
        sa.UniqueConstraint("public_token", name="uq_studio_template_variables_token"),
    )
    now = datetime.utcnow()
    table = sa.table(
        "studio_template_variables",
        sa.column("key", sa.String),
        sa.column("value", sa.Text),
        sa.column("kind", sa.String),
        sa.column("is_builtin", sa.Boolean),
        sa.column("updated_at", sa.DateTime),
    )
    op.bulk_insert(
        table,
        [
            {"key": key, "value": None, "kind": kind, "is_builtin": True, "updated_at": now}
            for key, kind in _BUILTINS
        ],
    )


def downgrade() -> None:
    op.drop_table("studio_template_variables")
