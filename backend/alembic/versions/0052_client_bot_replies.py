"""Ответы клиента 2/4/5 и тексты бота.

Revision ID: 0052_client_bot_replies
Revises: 0051_studio_template_variables
Create Date: 2026-10-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_client_bot_replies"
down_revision = "0051_studio_template_variables"
branch_labels = None
depends_on = None

_DEFAULTS = (
    ("confirm_1", "Спасибо, запись подтверждена"),
    ("reschedule_2", "Передали администратору, с вами свяжутся, чтобы подобрать другое время"),
    ("cancel_3", "Передали администратору, с вами свяжутся"),
    ("contact_vk", "Администратор скоро напишет вам здесь"),
    ("contact_telegram", "Напишите нам: {{studio_telegram}}"),
    ("contact_max", "Напишите нам: {{studio_max}}"),
    ("directions_5", "Как добраться: {{address}}\n{{route_video}}"),
    (
        "hint",
        "Ответьте: 1 — подтвердить, 2 — перенести, 3 — отменить, 4 — написать администратору, 5 — как добраться",
    ),
    ("no_booking", "Не нашли активную запись, свяжитесь с нами"),
    (
        "no_booking_reschedule",
        "Не нашли активную запись, напишите 4, чтобы связаться с администратором",
    ),
)


def upgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("client_cancel_handled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("client_cancel_handled_by_user_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("client_reschedule_requested_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("client_reschedule_requested_via", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("client_reschedule_handled_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("client_reschedule_handled_by_user_id", sa.Integer(), nullable=True))
    op.create_table(
        "client_bot_reply_templates",
        sa.Column("key", sa.String(length=40), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )
    table = sa.table(
        "client_bot_reply_templates",
        sa.column("key", sa.String),
        sa.column("body", sa.Text),
    )
    op.bulk_insert(table, [{"key": key, "body": body} for key, body in _DEFAULTS])


def downgrade() -> None:
    op.drop_table("client_bot_reply_templates")
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("client_reschedule_handled_by_user_id")
        batch.drop_column("client_reschedule_handled_at")
        batch.drop_column("client_reschedule_requested_via")
        batch.drop_column("client_reschedule_requested_at")
        batch.drop_column("client_cancel_handled_by_user_id")
        batch.drop_column("client_cancel_handled_at")
