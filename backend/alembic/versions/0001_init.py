"""Single full schema (dev): clients, visits, розница материала, комплекты.

Revision ID: 0001_init
Revises:
Create Date: 2026-03-31

service_categories.include_in_visit — участие категории в форме визита мастера.
services.order_rubber_extra_time_amort — услуга «Заказ» / резинки (длительность + амортизация).
services.retail_material_* — флаги розницы материала (канекалон / кудри / смешка).
product_sales — поля материала: граммы по типам, снимки цен, смешка, ручная себестоимость, material_cost_review_pending.

Порядок значений mixsource в схеме: NO_MIX, FROM_STOCK, SELF_MIXED (без смешки первым).

Для ранней разработки: удалить файл SQLite и снова выполнить `alembic upgrade head`.
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
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column(
            "role",
            sa.Enum("ADMIN_SUPER", "ADMIN", "MASTER", name="userrole"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "master_level",
            sa.Enum("JUNIOR", "MIDDLE", "SENIOR", name="masterlevel"),
            nullable=True,
        ),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
    )

    op.create_table(
        "user_role_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Enum("ADMIN_SUPER", "ADMIN", "MASTER", name="userrole"), nullable=False),
        sa.UniqueConstraint("user_id", "role", name="uq_user_role_assignments_user_role"),
    )

    op.create_table(
        "user_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "clients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("photo_1", sa.String(length=300), nullable=True),
        sa.Column("photo_2", sa.String(length=300), nullable=True),
        sa.Column("telegram", sa.String(length=100), nullable=True),
        sa.Column("vk", sa.String(length=120), nullable=True),
        sa.Column("instagram", sa.String(length=120), nullable=True),
        sa.Column("other_contact", sa.String(length=200), nullable=True),
        sa.Column(
            "age_group",
            sa.Enum("U10", "10_18", "18_30", "30_50", "50P", name="clientagegroup"),
            nullable=True,
        ),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("source_other", sa.String(length=200), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("birth_day", sa.Integer(), nullable=True),
        sa.Column("birth_month", sa.Integer(), nullable=True),
        sa.Column("birth_year", sa.Integer(), nullable=True),
        sa.Column("created_by_label", sa.String(length=240), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )

    op.create_table(
        "client_thermo_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("template_json", sa.Text(), nullable=False),
    )

    op.create_table(
        "studio_expense_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("name", name="uq_expense_category_name"),
    )

    op.create_table(
        "studio_expense_subcategories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("studio_expense_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("category_id", "name", name="uq_studio_expense_subcat_cat_name"),
    )

    op.create_table(
        "studio_expenses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("date", sa.DateTime(), nullable=False),
        sa.Column(
            "subcategory_id",
            sa.Integer(),
            sa.ForeignKey("studio_expense_subcategories.id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("is_voided", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )

    op.create_table(
        "service_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "include_in_visit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.UniqueConstraint("name", name="uq_service_category_name"),
    )

    questionnaire_field_type = sa.Enum(
        "TEXT", "NUMBER", "TEXTAREA", "CHECKBOX", "SELECT", name="questionnairefieldtype"
    )

    op.create_table(
        "service_subcategories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("service_categories.id"), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("show_kit_section", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("show_material_description", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("show_thermo_visit", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
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
        sa.Column("kit_section_override", sa.Boolean(), nullable=True),
        sa.Column("hide_material_description", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "order_rubber_extra_time_amort",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("master_pay_amount", sa.Float(), nullable=True),
        sa.Column("studio_pay_amount", sa.Float(), nullable=True),
        sa.Column("fixed_expense_amount", sa.Float(), nullable=True),
        sa.Column("is_per_unit", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("unit_label", sa.String(length=60), nullable=True),
        sa.Column(
            "retail_material_kanekalon",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "retail_material_kudri",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "retail_material_mix",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("subcategory_id", "name", name="uq_service_per_subcategory"),
    )

    # ---- Бронь (для админа): будущий визит/продажа ----
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("planned_date", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cancelled_reason", sa.Text(), nullable=True),
        sa.Column("quoted_price_text", sa.String(length=120), nullable=True),
        sa.Column("deposit_amount", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("planned_service_id", sa.Integer(), sa.ForeignKey("services.id"), nullable=True),
        sa.Column("planned_product_kind", sa.String(length=16), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
    )

    op.create_table(
        "booking_masters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "booking_id",
            sa.Integer(),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("master_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("booking_id", "master_id", name="uq_booking_master"),
    )

    op.create_table(
        "booking_staff",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "booking_id",
            sa.Integer(),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.UniqueConstraint("booking_id", "user_id", "kind", name="uq_booking_staff"),
    )

    op.create_table(
        "booking_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "booking_id",
            sa.Integer(),
            sa.ForeignKey("bookings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "category_questionnaire_fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("service_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(length=100), nullable=False),
        sa.Column("field_type", questionnaire_field_type, nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("placeholder", sa.String(length=500), nullable=True),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("options_json", sa.Text(), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("visibility_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("category_id", "field_key", name="uq_category_questionnaire_field_key"),
    )

    op.create_table(
        "subcategory_questionnaire_fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subcategory_id",
            sa.Integer(),
            sa.ForeignKey("service_subcategories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(length=100), nullable=False),
        sa.Column("field_type", questionnaire_field_type, nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("placeholder", sa.String(length=500), nullable=True),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("options_json", sa.Text(), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("visibility_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("subcategory_id", "field_key", name="uq_subcategory_questionnaire_field_key"),
    )

    op.create_table(
        "service_questionnaire_fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_key", sa.String(length=100), nullable=False),
        sa.Column("field_type", questionnaire_field_type, nullable=False),
        sa.Column("label", sa.String(length=500), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("placeholder", sa.String(length=500), nullable=True),
        sa.Column("help_text", sa.Text(), nullable=True),
        sa.Column("options_json", sa.Text(), nullable=True),
        sa.Column("min_value", sa.Float(), nullable=True),
        sa.Column("max_value", sa.Float(), nullable=True),
        sa.Column("visibility_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("service_id", "field_key", name="uq_service_questionnaire_field_key"),
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
        sa.Column("blank_type_de", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("blank_type_se", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("photo_1", sa.String(length=300), nullable=True),
        sa.Column("weight_grams", sa.Float(), nullable=True),
        sa.Column("length_cm", sa.Float(), nullable=True),
        sa.Column("has_decorations", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("materials_text", sa.Text(), nullable=True),
        sa.Column("color_text", sa.String(length=200), nullable=True),
        sa.Column("blanks_kinds_text", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("stock_price_total", sa.Float(), nullable=True),
        sa.Column("discount_percent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_total", sa.Float(), nullable=True),
        sa.Column("author_cost_total", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("(datetime('now'))"),
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_in_stock", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("author_external", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("sku", name="uq_kits_sku"),
    )

    op.create_table(
        "kit_reserves",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kit_id", sa.Integer(), sa.ForeignKey("kits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pieces_reserved", sa.Integer(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(), nullable=False),
        sa.Column("reserved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reserved_for_client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("reserved_for_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
    )

    op.create_table(
        "kit_author_staff",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kit_id", sa.Integer(), sa.ForeignKey("kits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("kit_id", "user_id", name="uq_kit_author_staff_kit_user"),
    )

    op.create_table(
        "visits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("is_cancelled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("performed_date", sa.DateTime(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id"), nullable=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column(
            "client_type",
            sa.Enum("NEW", "RETURNING", "SELF", name="visitclienttype"),
            nullable=False,
        ),
        sa.Column(
            "price_type",
            sa.Enum("CLIENT", "MODEL", name="visitpricetype"),
            nullable=False,
        ),
        sa.Column(
            "client_age_group",
            sa.Enum("U10", "10_18", "18_30", "30_50", "50P", name="clientagegroup"),
            nullable=True,
        ),
        sa.Column("kanekalon_grams", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("kudri_grams", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "mix_source",
            sa.Enum("NO_MIX", "FROM_STOCK", "SELF_MIXED", name="mixsource"),
            nullable=True,
        ),
        sa.Column(
            "mix_complexity",
            sa.Enum("SIMPLE", "MEDIUM", "HARD", name="mixcomplexity"),
            nullable=True,
        ),
        sa.Column("mix_cost_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("mix_bonus_master_id", sa.Integer(), nullable=True),
        sa.Column("mix_bonus_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("kanekalon_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("kudri_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("materials_cost_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("amount_from_client", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("client_discount_percent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("photo_1", sa.String(length=300), nullable=True),
        sa.Column("photo_2", sa.String(length=300), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("addons_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("addons_details_json", sa.Text(), nullable=True),
        sa.Column(
            "amortization_level",
            sa.Enum("MIN", "MID", "MAX", name="amortizationlevel"),
            nullable=True,
        ),
        sa.Column("amortization_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("studio_fund_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("profit_before_split", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("salon_cut_pct_at_time", sa.Float(), nullable=False, server_default=sa.text("0.3")),
        sa.Column("salon_profit", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("masters_pool", sa.Float(), nullable=False, server_default=sa.text("0")),
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

    op.create_table(
        "visit_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("visit_id", sa.Integer(), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "client_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "kit_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kit_id", sa.Integer(), sa.ForeignKey("kits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "product_sale_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("product_sales.id", ondelete="CASCADE"), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "studio_expense_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "expense_id",
            sa.Integer(),
            sa.ForeignKey("studio_expenses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "work_for_inventory_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "work_id",
            sa.Integer(),
            sa.ForeignKey("work_for_inventory.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "setting_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "setting_key",
            sa.String(length=100),
            sa.ForeignKey("settings.key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "work_rate_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "work_rate_id",
            sa.Integer(),
            sa.ForeignKey("work_rates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "service_category_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "category_id",
            sa.Integer(),
            sa.ForeignKey("service_categories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "service_subcategory_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subcategory_id",
            sa.Integer(),
            sa.ForeignKey("service_subcategories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "service_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "service_id",
            sa.Integer(),
            sa.ForeignKey("services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "category_questionnaire_field_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "field_id",
            sa.Integer(),
            sa.ForeignKey("category_questionnaire_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "subcategory_questionnaire_field_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "field_id",
            sa.Integer(),
            sa.ForeignKey("subcategory_questionnaire_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.create_table(
        "service_questionnaire_field_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "field_id",
            sa.Integer(),
            sa.ForeignKey("service_questionnaire_fields.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
    )

    op.execute(
        """
        UPDATE services SET order_rubber_extra_time_amort = 1
        WHERE name IN ('Прикрепление хвоста', 'Брейд под хвост')
        """
    )

    # ---- Продажа товаров (без услуги): материал/комплект/резинки/другое ----
    op.create_table(
        "product_sales",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("performed_date", sa.DateTime(), nullable=False),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("amount_from_client", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id"), nullable=True),
        sa.Column("is_voided", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "kind",
            sa.Enum("MATERIAL", "KIT", "RUBBER", "OTHER", name="productsalekind"),
            nullable=False,
        ),
        sa.Column("material_service_id", sa.Integer(), sa.ForeignKey("services.id"), nullable=True),
        sa.Column("material_grams", sa.Float(), nullable=True),
        sa.Column("material_description", sa.Text(), nullable=True),
        sa.Column("material_kanekalon_grams", sa.Float(), nullable=True),
        sa.Column("material_kudri_grams", sa.Float(), nullable=True),
        sa.Column("material_kanekalon_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("material_kudri_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("material_manual_cost", sa.Float(), nullable=True),
        sa.Column(
            "material_mix_source",
            sa.Enum("NO_MIX", "FROM_STOCK", "SELF_MIXED", name="mixsource"),
            nullable=True,
        ),
        sa.Column(
            "material_mix_complexity",
            sa.Enum("SIMPLE", "MEDIUM", "HARD", name="mixcomplexity"),
            nullable=True,
        ),
        sa.Column(
            "material_mix_cost_amount",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "material_mix_bonus_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "material_mix_bonus_amount",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("material_mix_standalone_grams", sa.Float(), nullable=True),
        sa.Column(
            "material_cost_review_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("kit_id", sa.Integer(), sa.ForeignKey("kits.id"), nullable=True),
        sa.Column("kit_pieces_sold", sa.Integer(), nullable=True),
        sa.Column("rubber_description", sa.Text(), nullable=True),
        sa.Column("rubber_price_override", sa.Integer(), nullable=True),
        sa.Column("other_description", sa.Text(), nullable=True),
        sa.Column("studio_margin_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
    )

    # ---- Прайс «Товары» (вне визита) ----
    op.create_table(
        "catalog_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("category_name", sa.String(length=120), nullable=False),
        sa.Column("subcategory_name", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # ---- «Работа с товарами»: история + участники ----
    op.create_table(
        "work_for_inventory",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("display_number", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("performed_date", sa.DateTime(), nullable=True),
        sa.Column("is_voided", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_kit_id", sa.Integer(), sa.ForeignKey("kits.id"), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id"), nullable=True),
        sa.Column("client_id", sa.Integer(), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("amount_from_client", sa.Integer(), nullable=True),
        sa.Column("ready_date", sa.DateTime(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("kanekalon_grams", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("kudri_grams", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "mix_source",
            sa.Enum("NO_MIX", "FROM_STOCK", "SELF_MIXED", name="mixsource"),
            nullable=True,
        ),
        sa.Column("kanekalon_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("kudri_price_per_gram_at_time", sa.Float(), nullable=True),
        sa.Column("materials_cost_total", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("extra_costs_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_total_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("master_profit_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("studio_profit_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("profit_total_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("studio_share_snapshot", sa.Numeric(3, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("rates_snapshot_json", sa.Text(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
    )

    op.create_table(
        "work_for_inventory_staff",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "work_id",
            sa.Integer(),
            sa.ForeignKey("work_for_inventory.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("share", sa.Numeric(3, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("master_profit_amount", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.UniqueConstraint("work_id", "user_id", name="uq_work_for_inventory_staff_work_user"),
    )

    # ---- Настройки ставок работ (JSON) ----
    op.create_table(
        "work_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("key", name="uq_work_rates_key"),
    )

    # ---- Журнал фондов ЗП ----
    op.create_table(
        "payroll_fund_ledger",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("entry_kind", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "storno_of_id",
            sa.Integer(),
            sa.ForeignKey("payroll_fund_ledger.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("payout_payment_kind", sa.String(length=20), nullable=True),
    )

    # ---- Payroll периоды (каркас) ----
    op.create_table(
        "payroll_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date_from", sa.DateTime(), nullable=False),
        sa.Column("date_to", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("closed_by_name", sa.String(length=200), nullable=True),
        sa.Column("closed_by_role", sa.String(length=50), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("payroll_periods")
    op.drop_table("payroll_fund_ledger")
    op.drop_table("service_questionnaire_field_audit_logs")
    op.drop_table("subcategory_questionnaire_field_audit_logs")
    op.drop_table("category_questionnaire_field_audit_logs")
    op.drop_table("service_audit_logs")
    op.drop_table("service_subcategory_audit_logs")
    op.drop_table("service_category_audit_logs")
    op.drop_table("work_rate_audit_logs")
    op.drop_table("setting_audit_logs")
    op.drop_table("work_for_inventory_audit_logs")
    op.drop_table("studio_expense_audit_logs")
    op.drop_table("work_rates")
    op.drop_table("work_for_inventory_staff")
    op.drop_table("work_for_inventory")
    op.drop_table("catalog_products")
    op.drop_table("product_sales")
    op.drop_table("booking_audit_logs")
    op.drop_table("booking_staff")
    op.drop_table("booking_masters")
    op.drop_table("bookings")
    op.drop_table("visit_audit_logs")
    op.drop_table("visit_kit_usages")
    op.drop_table("visit_services")
    op.drop_table("visit_masters")
    op.drop_table("visits")
    op.drop_table("kit_author_staff")
    op.drop_table("kit_reserves")
    op.drop_table("kits")
    op.drop_table("material_prices_current")
    op.drop_table("service_questionnaire_fields")
    op.drop_table("subcategory_questionnaire_fields")
    op.drop_table("category_questionnaire_fields")
    op.drop_table("services")
    op.drop_table("service_subcategories")
    op.drop_table("service_categories")
    op.drop_table("studio_expenses")
    op.drop_table("studio_expense_subcategories")
    op.drop_table("studio_expense_categories")
    op.drop_table("client_thermo_templates")
    op.drop_table("clients")
    op.drop_table("user_audit_logs")
    op.drop_table("user_role_assignments")
    op.drop_table("users")
    op.drop_table("settings")

    op.execute("DROP TYPE IF EXISTS userrole")
    op.execute("DROP TYPE IF EXISTS masterlevel")
    op.execute("DROP TYPE IF EXISTS clientagegroup")
    op.execute("DROP TYPE IF EXISTS visitclienttype")
    op.execute("DROP TYPE IF EXISTS visitpricetype")
    op.execute("DROP TYPE IF EXISTS mixsource")
    op.execute("DROP TYPE IF EXISTS mixcomplexity")
    op.execute("DROP TYPE IF EXISTS amortizationlevel")
    op.execute("DROP TYPE IF EXISTS materialtype")
    op.execute("DROP TYPE IF EXISTS questionnairefieldtype")
