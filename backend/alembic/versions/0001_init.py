"""init

Revision ID: 0001_init
Revises: 
Create Date: 2026-03-31
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.String(length=500), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.Enum("ADMIN", "MASTER", name="userrole"), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("master_level", sa.Enum("JUNIOR", "MIDDLE", "SENIOR", name="masterlevel"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("contact", sa.String(length=200), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
    )

    op.create_table(
        "studio_expense_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("name", name="uq_expense_category_name"),
    )

    op.create_table(
        "studio_expenses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.DateTime(), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("studio_expense_categories.id"), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
    )

    op.create_table(
        "service_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("name", name="uq_service_category_name"),
    )

    op.create_table(
        "service_subcategories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("service_categories.id"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("category_id", "name", name="uq_subcategory_per_category"),
    )

    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subcategory_id", sa.Integer(), sa.ForeignKey("service_subcategories.id"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("price_junior_from", sa.Float(), nullable=True),
        sa.Column("price_junior_to", sa.Float(), nullable=True),
        sa.Column("price_middle_from", sa.Float(), nullable=True),
        sa.Column("price_middle_to", sa.Float(), nullable=True),
        sa.Column("price_senior_from", sa.Float(), nullable=True),
        sa.Column("price_senior_to", sa.Float(), nullable=True),
        sa.UniqueConstraint("subcategory_id", "name", name="uq_service_per_subcategory"),
    )

    op.create_table(
        "material_prices_current",
        sa.Column("material_type", sa.Enum("KANEKALON", "KUDRI", name="materialtype"), primary_key=True),
        sa.Column("price_per_gram", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "kits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("pieces_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pieces_available", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stock_price_total", sa.Float(), nullable=True),
        sa.Column("cost_total", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("(datetime('now'))"),
        ),
        sa.Column("is_in_stock", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("sku", name="uq_kits_sku"),
    )

    op.create_table(
        "visits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("performed_date", sa.DateTime(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("client_type", sa.Enum("NEW", "RETURNING", "SELF", name="visitclienttype"), nullable=False),
        sa.Column("price_type", sa.Enum("CLIENT", "MODEL", name="visitpricetype"), nullable=False),
        sa.Column("client_age_group", sa.Enum("U10", "10_18", "18_30", "30_50", "50P", name="clientagegroup"), nullable=True),
        sa.Column("client_source", sa.String(length=120), nullable=True),
        sa.Column("client_source_other", sa.String(length=200), nullable=True),
        sa.Column("client_comment", sa.Text(), nullable=True),
        sa.Column("materials_used", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("kanekalon_grams", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("kudri_grams", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("mix_source", sa.Enum("FROM_STOCK", "NO_MIX", "SELF_MIXED", name="mixsource"), nullable=True),
        sa.Column("kanekalon_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("kudri_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("materials_cost_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("amount_from_client", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("extra_cost_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("extra_cost_comment", sa.String(length=200), nullable=True),
        sa.Column("addons_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("addons_details_json", sa.Text(), nullable=True),
        sa.Column("cost_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("profit_before_split", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("salon_cut_pct_at_time", sa.Float(), nullable=False, server_default=sa.text("0.3")),
        sa.Column("salon_profit", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("masters_pool", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("comment", sa.Text(), nullable=True),
    )

    op.create_table(
        "visit_masters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("visit_id", sa.Integer(), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("master_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("percent", sa.Float(), nullable=False),
        sa.UniqueConstraint("visit_id", "master_id", name="uq_visit_master"),
    )

    op.create_table(
        "visit_services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("visit_id", sa.Integer(), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("service_id", sa.Integer(), sa.ForeignKey("services.id"), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("category_name", sa.String(length=120), nullable=False),
        sa.Column("subcategory_name", sa.String(length=160), nullable=False),
        sa.Column("service_name", sa.String(length=200), nullable=False),
    )

    op.create_table(
        "visit_kit_usages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("visit_id", sa.Integer(), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("kit_id", sa.Integer(), sa.ForeignKey("kits.id"), nullable=False),
        sa.Column("pieces_used", sa.Integer(), nullable=False),
        sa.Column("cost_amount", sa.Float(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("visit_kit_usages")
    op.drop_table("visit_services")
    op.drop_table("visit_masters")
    op.drop_table("visits")
    op.drop_table("kits")
    op.drop_table("material_prices_current")
    op.drop_table("services")
    op.drop_table("service_subcategories")
    op.drop_table("service_categories")
    op.drop_table("studio_expenses")
    op.drop_table("studio_expense_categories")
    op.drop_table("clients")
    op.drop_table("users")
    op.drop_table("settings")

    op.execute("DROP TYPE IF EXISTS userrole")
    op.execute("DROP TYPE IF EXISTS masterlevel")
    op.execute("DROP TYPE IF EXISTS clientagegroup")
    op.execute("DROP TYPE IF EXISTS visitclienttype")
    op.execute("DROP TYPE IF EXISTS visitpricetype")
    op.execute("DROP TYPE IF EXISTS mixsource")
    op.execute("DROP TYPE IF EXISTS materialtype")
