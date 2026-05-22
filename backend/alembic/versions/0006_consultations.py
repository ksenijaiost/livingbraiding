"""Консультации, multi-service визиты/брони.

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
    # --- consultations (fix 27) ---
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
    op.create_table(
        "consultation_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("consultation_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["consultation_id"], ["consultations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("consultation_id", "service_id", name="uq_consultation_service"),
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

    # --- multi-service booking ---
    op.add_column(
        "bookings",
        sa.Column("masters_scope", sa.String(length=16), nullable=False, server_default="VISIT"),
    )
    op.add_column(
        "bookings",
        sa.Column("same_master_shares_all_services", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "booking_planned_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planned_start_time", sa.Time(), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "booking_planned_service_masters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("booking_planned_service_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_planned_service_id"],
            ["booking_planned_services.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["master_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "booking_planned_service_id",
            "master_id",
            name="uq_booking_planned_service_master",
        ),
    )

    # --- multi-service visit header ---
    op.add_column(
        "visits",
        sa.Column("masters_scope", sa.String(length=16), nullable=False, server_default="VISIT"),
    )
    op.add_column(
        "visits",
        sa.Column("same_master_shares_all_services", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # --- visit_services line economics ---
    vs_cols = [
        ("sort_order", sa.Integer(), "0"),
        ("is_cancelled", sa.Boolean(), "false"),
        ("cancelled_at", sa.DateTime(), None),
        ("cancelled_by_user_id", sa.Integer(), None),
        ("amount_from_client", sa.Float(), "0"),
        ("client_discount_percent", sa.Integer(), "0"),
        ("kanekalon_grams", sa.Float(), "0"),
        ("kudri_grams", sa.Float(), "0"),
        ("mix_source", sa.String(length=32), None),
        ("mix_complexity", sa.String(length=32), None),
        ("mix_cost_amount", sa.Float(), "0"),
        ("mix_bonus_master_id", sa.Integer(), None),
        ("mix_bonus_amount", sa.Float(), "0"),
        ("kanekalon_price_per_gram_at_time", sa.Float(), None),
        ("kudri_price_per_gram_at_time", sa.Float(), None),
        ("materials_cost_total", sa.Float(), "0"),
        ("addons_total", sa.Float(), "0"),
        ("addons_details_json", sa.Text(), None),
        ("amortization_level", sa.String(length=16), None),
        ("amortization_amount", sa.Float(), "0"),
        ("studio_fund_amount", sa.Float(), "0"),
        ("cost_total", sa.Float(), "0"),
        ("profit_before_split", sa.Float(), "0"),
        ("salon_cut_pct_at_time", sa.Float(), "0.5"),
        ("salon_profit", sa.Float(), "0"),
        ("masters_pool", sa.Float(), "0"),
        ("kit_paid_separately", sa.Boolean(), "false"),
        ("started_at", sa.DateTime(), None),
        ("comment", sa.Text(), None),
    ]
    for name, col_type, default in vs_cols:
        kw: dict = {"nullable": False} if default is not None and col_type != sa.DateTime else {"nullable": True}
        if default is not None and name not in ("cancelled_at", "cancelled_by_user_id", "mix_source", "mix_complexity", "amortization_level", "started_at", "comment", "addons_details_json", "kanekalon_price_per_gram_at_time", "kudri_price_per_gram_at_time", "mix_bonus_master_id"):
            kw["server_default"] = default
        op.add_column("visit_services", sa.Column(name, col_type, **kw))
    op.create_foreign_key(
        "fk_visit_services_cancelled_by",
        "visit_services",
        "users",
        ["cancelled_by_user_id"],
        ["id"],
    )

    op.create_table(
        "visit_service_masters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("visit_service_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("percent", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["visit_service_id"], ["visit_services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["master_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("visit_service_id", "master_id", name="uq_visit_service_master"),
    )

    op.add_column("visit_kit_usages", sa.Column("visit_service_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_visit_kit_usages_visit_service_id",
        "visit_kit_usages",
        "visit_services",
        ["visit_service_id"],
        ["id"],
    )

    # --- backfill visit_services from visits (single line per visit) ---
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT v.id AS visit_id, vs.id AS vs_id
            FROM visits v
            JOIN visit_services vs ON vs.visit_id = v.id
            """
        )
    ).fetchall()
    for visit_id, vs_id in rows:
        conn.execute(
            sa.text(
                """
                UPDATE visit_services SET
                  sort_order = 0,
                  is_cancelled = (SELECT is_cancelled FROM visits WHERE id = :vid),
                  amount_from_client = (SELECT amount_from_client FROM visits WHERE id = :vid),
                  client_discount_percent = (SELECT client_discount_percent FROM visits WHERE id = :vid),
                  kanekalon_grams = (SELECT kanekalon_grams FROM visits WHERE id = :vid),
                  kudri_grams = (SELECT kudri_grams FROM visits WHERE id = :vid),
                  mix_cost_amount = (SELECT mix_cost_amount FROM visits WHERE id = :vid),
                  mix_bonus_master_id = (SELECT mix_bonus_master_id FROM visits WHERE id = :vid),
                  mix_bonus_amount = (SELECT mix_bonus_amount FROM visits WHERE id = :vid),
                  kanekalon_price_per_gram_at_time = (SELECT kanekalon_price_per_gram_at_time FROM visits WHERE id = :vid),
                  kudri_price_per_gram_at_time = (SELECT kudri_price_per_gram_at_time FROM visits WHERE id = :vid),
                  materials_cost_total = (SELECT materials_cost_total FROM visits WHERE id = :vid),
                  addons_total = (SELECT addons_total FROM visits WHERE id = :vid),
                  addons_details_json = (SELECT addons_details_json FROM visits WHERE id = :vid),
                  amortization_amount = (SELECT amortization_amount FROM visits WHERE id = :vid),
                  studio_fund_amount = (SELECT studio_fund_amount FROM visits WHERE id = :vid),
                  cost_total = (SELECT cost_total FROM visits WHERE id = :vid),
                  profit_before_split = (SELECT profit_before_split FROM visits WHERE id = :vid),
                  salon_cut_pct_at_time = (SELECT salon_cut_pct_at_time FROM visits WHERE id = :vid),
                  salon_profit = (SELECT salon_profit FROM visits WHERE id = :vid),
                  masters_pool = (SELECT masters_pool FROM visits WHERE id = :vid),
                  kit_paid_separately = (SELECT kit_paid_separately FROM visits WHERE id = :vid)
                WHERE id = :vsid
                """
            ),
            {"vid": visit_id, "vsid": vs_id},
        )
        conn.execute(
            sa.text(
                "UPDATE visit_kit_usages SET visit_service_id = :vsid WHERE visit_id = :vid"
            ),
            {"vid": visit_id, "vsid": vs_id},
        )

    conn.execute(
        sa.text(
            """
            INSERT INTO consultation_services (consultation_id, service_id, sort_order)
            SELECT id, service_id, 0 FROM consultations WHERE service_id IS NOT NULL
            """
        )
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO booking_planned_services (booking_id, service_id, sort_order)
            SELECT id, planned_service_id, 0 FROM bookings
            WHERE planned_service_id IS NOT NULL AND kind = 'VISIT'
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_visit_kit_usages_visit_service_id", "visit_kit_usages", type_="foreignkey")
    op.drop_column("visit_kit_usages", "visit_service_id")
    op.drop_table("visit_service_masters")
    for col in (
        "comment",
        "started_at",
        "kit_paid_separately",
        "masters_pool",
        "salon_profit",
        "salon_cut_pct_at_time",
        "profit_before_split",
        "cost_total",
        "studio_fund_amount",
        "amortization_amount",
        "amortization_level",
        "addons_details_json",
        "addons_total",
        "materials_cost_total",
        "kudri_price_per_gram_at_time",
        "kanekalon_price_per_gram_at_time",
        "mix_bonus_amount",
        "mix_bonus_master_id",
        "mix_cost_amount",
        "mix_complexity",
        "mix_source",
        "kudri_grams",
        "kanekalon_grams",
        "client_discount_percent",
        "amount_from_client",
        "cancelled_by_user_id",
        "cancelled_at",
        "is_cancelled",
        "sort_order",
    ):
        op.drop_column("visit_services", col)
    op.drop_constraint("fk_visit_services_cancelled_by", "visit_services", type_="foreignkey")
    op.drop_column("visits", "same_master_shares_all_services")
    op.drop_column("visits", "masters_scope")
    op.drop_table("booking_planned_service_masters")
    op.drop_table("booking_planned_services")
    op.drop_column("bookings", "same_master_shares_all_services")
    op.drop_column("bookings", "masters_scope")
    op.drop_constraint("uq_bookings_consultation_id", "bookings", type_="unique")
    op.drop_constraint("fk_bookings_consultation_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "consultation_id")
    op.drop_table("consultation_services")
    op.drop_table("consultation_audit_logs")
    op.drop_table("consultations")
