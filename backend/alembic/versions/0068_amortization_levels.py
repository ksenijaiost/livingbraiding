"""Уровни амортизации в настройках; amortization_level → String(32).

Revision ID: 0068_amortization_levels
Revises: 0067_studio_chats_and_internal_notify
Create Date: 2026-11-03
"""

from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0068_amortization_levels"
down_revision = "0067_studio_chats_and_internal_notify"
branch_labels = None
depends_on = None

_SEED = (
    ("MIN", "Минимальная", 100.0, None, 0),
    ("MID", "Средняя", 200.0, "включая украшения, резинки и пр.", 1),
    ("MAX", "Увеличенная", 500.0, "включая средства для волос и пр.", 2),
)
_KNOWN = frozenset({"MIN", "MID", "MAX"})


def upgrade() -> None:
    op.create_table(
        "amortization_levels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("code", name="uq_amortization_levels_code"),
    )
    op.create_table(
        "amortization_level_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "amortization_level_id",
            sa.Integer(),
            sa.ForeignKey("amortization_levels.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for code, name, amount, description, position in _SEED:
        op.execute(
            sa.text(
                """
                INSERT INTO amortization_levels
                    (code, name, amount, description, position, created_at)
                SELECT :code, :name, :amount, :description, :position, :created_at
                WHERE NOT EXISTS (
                    SELECT 1 FROM amortization_levels WHERE code = :code
                )
                """
            ).bindparams(
                code=code,
                name=name,
                amount=amount,
                description=description,
                position=position,
                created_at=now,
            )
        )

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "ALTER TABLE visits ALTER COLUMN amortization_level "
                "TYPE VARCHAR(32) USING amortization_level::text"
            )
        )
        op.execute(sa.text("DROP TYPE IF EXISTS amortizationlevel"))
        op.execute(
            sa.text(
                "ALTER TABLE visit_services ALTER COLUMN amortization_level "
                "TYPE VARCHAR(32)"
            )
        )
    else:
        with op.batch_alter_table("visits") as batch:
            batch.alter_column(
                "amortization_level",
                existing_type=sa.Enum("MIN", "MID", "MAX", name="amortizationlevel"),
                type_=sa.String(length=32),
                existing_nullable=True,
            )
        with op.batch_alter_table("visit_services") as batch:
            batch.alter_column(
                "amortization_level",
                existing_type=sa.String(length=16),
                type_=sa.String(length=32),
                existing_nullable=True,
            )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        rows = conn.execute(
            sa.text(
                "SELECT DISTINCT amortization_level FROM visits "
                "WHERE amortization_level IS NOT NULL"
            )
        ).fetchall()
        codes = {str(r[0]) for r in rows if r[0] is not None}
        svc_rows = conn.execute(
            sa.text(
                "SELECT DISTINCT amortization_level FROM visit_services "
                "WHERE amortization_level IS NOT NULL"
            )
        ).fetchall()
        codes |= {str(r[0]) for r in svc_rows if r[0] is not None}
        if codes - _KNOWN:
            raise RuntimeError(
                "Downgrade 0068 невозможен: в данных есть коды амортизации кроме MIN/MID/MAX."
            )
        enum = sa.Enum("MIN", "MID", "MAX", name="amortizationlevel")
        enum.create(conn, checkfirst=True)
        op.execute(
            sa.text(
                "ALTER TABLE visits ALTER COLUMN amortization_level "
                "TYPE amortizationlevel USING amortization_level::amortizationlevel"
            )
        )
        # visit_services остаётся строкой (так было с 0006) — только сужаем длину
        op.execute(
            sa.text(
                "ALTER TABLE visit_services ALTER COLUMN amortization_level "
                "TYPE VARCHAR(16)"
            )
        )
    else:
        with op.batch_alter_table("visits") as batch:
            batch.alter_column(
                "amortization_level",
                existing_type=sa.String(length=32),
                type_=sa.Enum("MIN", "MID", "MAX", name="amortizationlevel"),
                existing_nullable=True,
            )
        with op.batch_alter_table("visit_services") as batch:
            batch.alter_column(
                "amortization_level",
                existing_type=sa.String(length=32),
                type_=sa.String(length=16),
                existing_nullable=True,
            )

    op.drop_table("amortization_level_audit_logs")
    op.drop_table("amortization_levels")
