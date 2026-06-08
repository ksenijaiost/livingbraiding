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
        op.execute(
            """
            ALTER TABLE booking_planned_services
            ALTER COLUMN planned_start_time TYPE TIMESTAMP WITHOUT TIME ZONE
            USING (
                CASE
                    WHEN planned_start_time IS NULL THEN NULL
                    ELSE (
                        (SELECT date(b.planned_date) FROM bookings b WHERE b.id = booking_planned_services.booking_id)
                        + planned_start_time::time
                    )::timestamp without time zone
                END
            )
            """
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
        op.execute(
            """
            ALTER TABLE booking_planned_services
            ALTER COLUMN planned_start_time TYPE TIME WITHOUT TIME ZONE
            USING (
                CASE
                    WHEN planned_start_time IS NULL THEN NULL
                    ELSE planned_start_time::time without time zone
                END
            )
            """
        )
    else:
        op.alter_column(
            "booking_planned_services",
            "planned_start_time",
            existing_type=sa.DateTime(),
            type_=sa.Time(),
            existing_nullable=True,
        )
