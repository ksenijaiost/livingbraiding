"""Консультации и связь с бронями.

Revision ID: 0006_consultations
Revises: 0005_drop_kit_decor_blanks_kinds
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_consultations"
down_revision = "0005_drop_kit_decor_blanks_kinds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consultations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("consultation_date", sa.DateTime(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=True),
        sa.Column("types_json", sa.Text(), nullable=True),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("preliminary_cost_text", sa.String(length=120), nullable=True),
        sa.Column("photo_1", sa.String(length=300), nullable=True),
        sa.Column("photo_2", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "consultation_audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("consultation_id", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["changed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["consultation_id"], ["consultations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("bookings", sa.Column("consultation_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_bookings_consultation_id",
        "bookings",
        "consultations",
        ["consultation_id"],
        ["id"],
    )
    op.create_unique_constraint("uq_bookings_consultation_id", "bookings", ["consultation_id"])


def downgrade() -> None:
    op.drop_constraint("uq_bookings_consultation_id", "bookings", type_="unique")
    op.drop_constraint("fk_bookings_consultation_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "consultation_id")
    op.drop_table("consultation_audit_logs")
    op.drop_table("consultations")
