"""Типы уведомлений у users + перенос с notify_enabled.

Revision ID: 0045_user_notify_type_prefs
Revises: 0044_user_reminder_settings
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_user_notify_type_prefs"
down_revision = "0044_user_reminder_settings"
branch_labels = None
depends_on = None

_COLS = (
    "notify_bookings",
    "notify_visit",
    "notify_work",
    "notify_hourly_work",
    "notify_product_sale",
    "notify_consultation",
    "notify_work_plan",
)

# key → roles that unlock the type (same as notify_prefs.NOTIFY_TYPE_SPECS)
_ROLE_FOR_COL: dict[str, tuple[str, ...]] = {
    "notify_bookings": ("MASTER", "ADMIN", "ADMIN_SUPER"),
    "notify_visit": ("MASTER", "HELPER", "ADMIN", "ADMIN_SUPER"),
    "notify_work": ("MASTER",),
    "notify_hourly_work": ("MASTER", "HELPER"),
    "notify_product_sale": ("MASTER", "ADMIN", "ADMIN_SUPER"),
    "notify_consultation": ("MASTER", "ADMIN", "ADMIN_SUPER"),
    "notify_work_plan": ("MASTER", "HELPER"),
}


def upgrade() -> None:
    for col in _COLS:
        op.add_column(
            "users",
            sa.Column(col, sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )

    conn = op.get_bind()
    users = conn.execute(sa.text("SELECT id, notify_enabled FROM users")).fetchall()
    for user_id, notify_enabled in users:
        roles = {
            r[0]
            for r in conn.execute(
                sa.text("SELECT role FROM user_role_assignments WHERE user_id = :uid"),
                {"uid": int(user_id)},
            ).fetchall()
        }
        # fallback: denormalized users.role
        if not roles:
            row = conn.execute(
                sa.text("SELECT role FROM users WHERE id = :uid"),
                {"uid": int(user_id)},
            ).fetchone()
            if row and row[0]:
                roles = {str(row[0])}
        values: dict[str, bool] = {}
        for col, allowed in _ROLE_FOR_COL.items():
            values[col] = bool(notify_enabled) and bool(roles.intersection(allowed))
        any_on = any(values.values())
        conn.execute(
            sa.text(
                """
                UPDATE users SET
                  notify_bookings = :notify_bookings,
                  notify_visit = :notify_visit,
                  notify_work = :notify_work,
                  notify_hourly_work = :notify_hourly_work,
                  notify_product_sale = :notify_product_sale,
                  notify_consultation = :notify_consultation,
                  notify_work_plan = :notify_work_plan,
                  notify_enabled = :notify_enabled
                WHERE id = :uid
                """
            ),
            {
                **{k: bool(v) for k, v in values.items()},
                "notify_enabled": any_on,
                "uid": int(user_id),
            },
        )

    # server_default только для миграции существующих строк
    for col in _COLS:
        op.alter_column("users", col, server_default=None)


def downgrade() -> None:
    for col in reversed(_COLS):
        op.drop_column("users", col)
