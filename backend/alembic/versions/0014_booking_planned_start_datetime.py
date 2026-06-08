"""planned_start_time: Time → DateTime (UTC naive).

Revision ID: 0014_booking_planned_start_datetime
Revises: 0013_service_category_consultation_kind
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_booking_planned_start_datetime"
down_revision = "0013_service_category_consultation_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.add_column(
            "booking_planned_services",
            sa.Column("planned_start_time_new", sa.DateTime(), nullable=True),
        )
        op.execute(
            """
            UPDATE booking_planned_services ps
            SET planned_start_time_new = (
                date(b.planned_date) + ps.planned_start_time::time
            )::timestamp without time zone
            FROM bookings b
            WHERE b.id = ps.booking_id
              AND ps.planned_start_time IS NOT NULL
            """
        )
        op.drop_column("booking_planned_services", "planned_start_time")
        op.alter_column(
            "booking_planned_services",
            "planned_start_time_new",
            new_column_name="planned_start_time",
        )
    else:
        op.alter_column(
            "booking_planned_services",
            "planned_start_time",
            existing_type=sa.Time(),
            type_=sa.DateTime(),
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.add_column(
            "booking_planned_services",
            sa.Column("planned_start_time_old", sa.Time(), nullable=True),
        )
        op.execute(
            """
            UPDATE booking_planned_services
            SET planned_start_time_old = planned_start_time::time without time zone
            WHERE planned_start_time IS NOT NULL
            """
        )
        op.drop_column("booking_planned_services", "planned_start_time")
        op.alter_column(
            "booking_planned_services",
            "planned_start_time_old",
            new_column_name="planned_start_time",
        )
    else:
        op.alter_column(
            "booking_planned_services",
            "planned_start_time",
            existing_type=sa.DateTime(),
            type_=sa.Time(),
            existing_nullable=True,
        )
