from __future__ import annotations

"""
SQLAlchemy ORM models.

Key principle: **no historical recalculation**.

Anything that can change over time (prices, salon %, etc.) must be stored as a *snapshot*
inside the visit-related tables, so changing settings only affects *future* visits.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UserRole(str, enum.Enum):
    ADMIN_SUPER = "ADMIN_SUPER"
    ADMIN = "ADMIN"
    MASTER = "MASTER"


class MasterLevel(str, enum.Enum):
    JUNIOR = "JUNIOR"
    MIDDLE = "MIDDLE"
    SENIOR = "SENIOR"


class ClientAgeGroup(str, enum.Enum):
    U10 = "U10"
    A10_18 = "10_18"
    A18_30 = "18_30"
    A30_50 = "30_50"
    A50P = "50P"


class VisitClientType(str, enum.Enum):
    NEW = "NEW"
    RETURNING = "RETURNING"
    SELF = "SELF"


class VisitPriceType(str, enum.Enum):
    CLIENT = "CLIENT"
    MODEL = "MODEL"


class MixSource(str, enum.Enum):
    NO_MIX = "NO_MIX"
    FROM_STOCK = "FROM_STOCK"
    SELF_MIXED = "SELF_MIXED"


class MixComplexity(str, enum.Enum):
    SIMPLE = "SIMPLE"  # 1 ₽/г
    MEDIUM = "MEDIUM"  # 1.5 ₽/г
    HARD = "HARD"  # 2 ₽/г


class AmortizationLevel(str, enum.Enum):
    MIN = "MIN"  # 100 ₽
    MID = "MID"  # 200 ₽
    MAX = "MAX"  # 500 ₽


class QuestionnaireFieldType(str, enum.Enum):
    """Тип поля опросника (анкета мастера, шаг 4)."""

    TEXT = "TEXT"
    NUMBER = "NUMBER"
    TEXTAREA = "TEXTAREA"
    CHECKBOX = "CHECKBOX"
    SELECT = "SELECT"


class MaterialType(str, enum.Enum):
    KANEKALON = "KANEKALON"
    KUDRI = "KUDRI"


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    master_level: Mapped[MasterLevel | None] = mapped_column(Enum(MasterLevel), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    telegram: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vk: Mapped[str | None] = mapped_column(String(120), nullable=True)
    instagram: Mapped[str | None] = mapped_column(String(120), nullable=True)
    other_contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    age_group: Mapped[ClientAgeGroup | None] = mapped_column(Enum(ClientAgeGroup), nullable=True)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_other: Mapped[str | None] = mapped_column(String(200), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Optional birthday: all null = unknown; day+month without year = "only DM"
    birth_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    birth_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Snapshot at creation: e.g. "Анна (MASTER)" — set by app from current user display_name + role
    created_by_label: Mapped[str | None] = mapped_column(String(240), nullable=True)

    thermo_templates: Mapped[list["ClientThermoTemplate"]] = relationship(
        back_populates="client",
        order_by="ClientThermoTemplate.id",
    )


class ClientThermoTemplate(Base):
    """Сохранённый шаблон термозамещения клиента (для выбора «Старый» во визите)."""

    __tablename__ = "client_thermo_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    template_json: Mapped[str] = mapped_column(Text, nullable=False)

    client: Mapped["Client"] = relationship(back_populates="thermo_templates")


class StudioExpenseCategory(Base):
    __tablename__ = "studio_expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    subcategories: Mapped[list["StudioExpenseSubcategory"]] = relationship(
        back_populates="category",
        order_by="StudioExpenseSubcategory.sort_order",
    )


class StudioExpenseSubcategory(Base):
    __tablename__ = "studio_expense_subcategories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("studio_expense_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    category: Mapped["StudioExpenseCategory"] = relationship(back_populates="subcategories")

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_studio_expense_subcat_cat_name"),)


class StudioExpense(Base):
    __tablename__ = "studio_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    subcategory_id: Mapped[int] = mapped_column(
        ForeignKey("studio_expense_subcategories.id"),
        nullable=False,
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")

    is_voided: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    voided_by_user: Mapped["User | None"] = relationship(foreign_keys=[voided_by_user_id])
    subcategory: Mapped["StudioExpenseSubcategory"] = relationship()


class ServiceCategory(Base):
    __tablename__ = "service_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # False: не в форме визита (напр. продажа материала — отдельный поток на этапе 7).
    include_in_visit: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    questionnaire_fields: Mapped[list["CategoryQuestionnaireField"]] = relationship(
        back_populates="category",
        order_by="CategoryQuestionnaireField.sort_order",
    )


class CategoryQuestionnaireField(Base):
    """Поля анкеты, общие для всех услуг категории (до полей подкатегории и услуги)."""

    __tablename__ = "category_questionnaire_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("service_categories.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[QuestionnaireFieldType] = mapped_column(Enum(QuestionnaireFieldType), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    placeholder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    help_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    visibility_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped[ServiceCategory] = relationship(back_populates="questionnaire_fields")

    __table_args__ = (
        UniqueConstraint("category_id", "field_key", name="uq_category_questionnaire_field_key"),
    )


class ServiceSubcategory(Base):
    __tablename__ = "service_subcategories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("service_categories.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Форма визита: блок «Комплект» (склад) для услуг этой подкатегории, если у услуги нет своего override.
    show_kit_section: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Показывать поле «Описание про материал» из анкеты категории (если оно задано на категории).
    show_material_description: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Расширенный блок термозамещения на шаге 2 визита + шаблоны клиента (без общих полей категории).
    show_thermo_visit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    category: Mapped[ServiceCategory] = relationship()
    questionnaire_fields: Mapped[list[SubcategoryQuestionnaireField]] = relationship(
        back_populates="subcategory",
        order_by="SubcategoryQuestionnaireField.sort_order",
    )

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_subcategory_per_category"),)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(ForeignKey("service_subcategories.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Прайс по уровням мастера (в UI: младший / мастер / старший); любой диапазон может быть NULL.
    price_junior_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_junior_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_middle_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_middle_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_senior_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_senior_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    # None — брать из подкатегории show_kit_section; True/False — принудительно.
    kit_section_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Не показывать «Описание про материал» даже если подкатегория позволяет.
    hide_material_description: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    order_rubber_extra_time_amort: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Экономика (для суперадмина): используется для автоподсчёта ЗП/студии/расходов.
    # Если is_per_unit=True — значения ниже считаются "за 1 единицу" (крепление/коса/шт).
    master_pay_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    studio_pay_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    fixed_expense_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_per_unit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unit_label: Mapped[str | None] = mapped_column(String(60), nullable=True)

    subcategory: Mapped[ServiceSubcategory] = relationship()
    questionnaire_fields: Mapped[list[ServiceQuestionnaireField]] = relationship(
        back_populates="service",
        order_by="ServiceQuestionnaireField.sort_order",
    )

    __table_args__ = (UniqueConstraint("subcategory_id", "name", name="uq_service_per_subcategory"),)


class SubcategoryQuestionnaireField(Base):
    """
    Поля анкеты, общие для всех услуг подкатегории.
    `field_key` уникален в рамках подкатегории; при склейке с полями услуги ключи не должны пересекаться
    (проверка на сохранении / при сборке формы).
    """

    __tablename__ = "subcategory_questionnaire_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(
        ForeignKey("service_subcategories.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[QuestionnaireFieldType] = mapped_column(Enum(QuestionnaireFieldType), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    placeholder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    help_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    visibility_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    subcategory: Mapped[ServiceSubcategory] = relationship(back_populates="questionnaire_fields")

    __table_args__ = (
        UniqueConstraint("subcategory_id", "field_key", name="uq_subcategory_questionnaire_field_key"),
    )


class ServiceQuestionnaireField(Base):
    """Дополнительные поля анкеты только для этой услуги (к общим полям подкатегории)."""

    __tablename__ = "service_questionnaire_fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[QuestionnaireFieldType] = mapped_column(Enum(QuestionnaireFieldType), nullable=False)
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    placeholder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    help_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    options_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    visibility_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    service: Mapped[Service] = relationship(back_populates="questionnaire_fields")

    __table_args__ = (
        UniqueConstraint("service_id", "field_key", name="uq_service_questionnaire_field_key"),
    )


class MaterialPriceCurrent(Base):
    __tablename__ = "material_prices_current"

    # This table stores current prices used as defaults for new visits.
    # Visits store snapshots (`*_price_per_gram_at_time`) and must never be recalculated.
    material_type: Mapped[MaterialType] = mapped_column(Enum(MaterialType), primary_key=True)
    price_per_gram: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Kit(Base):
    """
    Один комплект = одна строка: карточка, остаток заготовок, цены.
    Заготовки списываются поштучно или целиком; когда кончились — `is_in_stock=False`.
    `is_archived=True` — не показывать в выборе «из наличия» (история и прошлые визиты не трогаются).
    """

    __tablename__ = "kits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    pieces_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pieces_available: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Карточка склада (шаг 3.3): типы заготовок — можно один или оба.
    blank_type_de: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    blank_type_se: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    weight_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_decorations: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    materials_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    color_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    blanks_kinds_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Из наличия: вычитаемая из прибыли визита цена (пропорционально списанным заготовкам).
    stock_price_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Скидка с цены комплекта, целые проценты 0–100; рубли = цена × (процент/100) при списании.
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Себестоимость всего комплекта: затраты + ЗП авторов (едино поле; author_cost_total не используем в расчётах).
    cost_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Устарело: не участвует в формуле фонда студии (себестоимость включает ЗП авторов).
    author_cost_total: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    is_in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Резерв (MVP: одна метка, без истории). Заполняется при «зарезервировать», очищается при «снять».
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reserved_by_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    reserved_for_client_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clients.id"), nullable=True)
    reserved_for_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)

    reserved_by_user: Mapped[User | None] = relationship(foreign_keys=[reserved_by_user_id])
    reserved_for_client: Mapped[Client | None] = relationship(foreign_keys=[reserved_for_client_id])
    reserved_for_user: Mapped[User | None] = relationship(foreign_keys=[reserved_for_user_id])

    # Автор(ы) комплекта: сотрудники студии и/или отметка «Извне» (заполняется при внесении карточки).
    author_external: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    author_staff_links: Mapped[list["KitAuthorStaff"]] = relationship(
        back_populates="kit",
        cascade="all, delete-orphan",
        order_by="KitAuthorStaff.sort_order",
    )

    @property
    def is_reserved(self) -> bool:
        return self.reserved_at is not None


class KitAuthorStaff(Base):
    """Связь комплект — сотрудник (авторство); «Извне» — флаг `Kit.author_external`."""

    __tablename__ = "kit_author_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    kit: Mapped["Kit"] = relationship(back_populates="author_staff_links")
    user: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("kit_id", "user_id", name="uq_kit_author_staff_kit_user"),)


class CatalogProduct(Base):
    """
    Прайс «Товары» (вне визита).
    Категории/подкатегории — для UI-таблицы и фильтрации.
    """

    __tablename__ = "catalog_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    category_name: Mapped[str] = mapped_column(String(120), nullable=False)
    subcategory_name: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProductSaleKind(str, enum.Enum):
    MATERIAL = "MATERIAL"
    KIT = "KIT"
    RUBBER = "RUBBER"
    OTHER = "OTHER"


class ProductSale(Base):
    """Продажа товара без услуги (розница): материал/комплект/резинки/другое."""

    __tablename__ = "product_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    amount_from_client: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_voided: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    kind: Mapped[ProductSaleKind] = mapped_column(
        Enum(ProductSaleKind, native_enum=False, length=16),
        nullable=False,
    )

    # MATERIAL
    material_service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"), nullable=True)
    material_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # KIT
    kit_id: Mapped[int | None] = mapped_column(ForeignKey("kits.id"), nullable=True)
    kit_pieces_sold: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # RUBBER
    rubber_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubber_price_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # OTHER
    other_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    voided_by_user: Mapped["User | None"] = relationship(foreign_keys=[voided_by_user_id])
    client: Mapped["Client"] = relationship()
    material_service: Mapped["Service | None"] = relationship(foreign_keys=[material_service_id])
    kit: Mapped["Kit | None"] = relationship(foreign_keys=[kit_id])


class WorkScope(str, enum.Enum):
    IN_STOCK = "IN_STOCK"
    CUSTOM_ORDER = "CUSTOM_ORDER"


class WorkKind(str, enum.Enum):
    KIT = "KIT"
    MIX = "MIX"
    RUBBER = "RUBBER"
    KIT_CORRECTION = "KIT_CORRECTION"
    HAIR_EXT_PREP = "HAIR_EXT_PREP"


class WorkForInventory(Base):
    """История «работа с товарами» (в наличие / на заказ)."""

    __tablename__ = "work_for_inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_number: Mapped[str | None] = mapped_column(String(40), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    kind: Mapped[WorkKind] = mapped_column(
        Enum(WorkKind, native_enum=False, length=32),
        nullable=False,
    )
    scope: Mapped[WorkScope] = mapped_column(
        Enum(WorkScope, native_enum=False, length=24),
        nullable=False,
    )

    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    amount_from_client: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ready_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Материал (как в визите) + снимки цен/ставок на момент записи
    kanekalon_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    kudri_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mix_source: Mapped[MixSource | None] = mapped_column(Enum(MixSource), nullable=True)
    kanekalon_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    kudri_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    materials_cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Деньги (всё — снимки, без пересчёта задним числом)
    extra_costs_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_total_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    master_profit_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    studio_profit_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_total_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    studio_share_snapshot: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0)
    rates_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    client: Mapped["Client | None"] = relationship()
    staff_rows: Mapped[list["WorkForInventoryStaff"]] = relationship(
        back_populates="work",
        cascade="all, delete-orphan",
    )


class WorkForInventoryStaff(Base):
    __tablename__ = "work_for_inventory_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("work_for_inventory.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    share: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=0)
    master_profit_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    work: Mapped["WorkForInventory"] = relationship(back_populates="staff_rows")
    user: Mapped["User"] = relationship()

    __table_args__ = (
        UniqueConstraint("work_id", "user_id", name="uq_work_for_inventory_staff_work_user"),
    )


class WorkRate(Base):
    """Справочник ставок/коэффициентов (JSON) для «работ с товарами»."""

    __tablename__ = "work_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])


class PayrollPeriod(Base):
    """Периоды начисления/закрытия (каркас; проводки будут позже)."""

    __tablename__ = "payroll_periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_from: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    date_to: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    closed_by_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class StudioOrderSubcategoryKey(str, enum.Enum):
    """Подкатегория каталога «Заказ» (одна на запись)."""

    KOMPLEKT = "KOMPLEKT"
    ZAGOTOVKI = "ZAGOTOVKI"
    REZINKI = "REZINKI"
    KORREKTSIYA = "KORREKTSIYA"


class StudioOrder(Base):
    """Заказ (категория «Заказ»), вне формы визита; расчёт прибыли как у визита."""

    __tablename__ = "studio_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    client_type: Mapped[VisitClientType] = mapped_column(Enum(VisitClientType), nullable=False)
    price_type: Mapped[VisitPriceType] = mapped_column(Enum(VisitPriceType), nullable=False)
    client_age_group: Mapped[ClientAgeGroup | None] = mapped_column(Enum(ClientAgeGroup), nullable=True)

    kanekalon_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    kudri_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mix_source: Mapped[MixSource | None] = mapped_column(Enum(MixSource), nullable=True)
    mix_complexity: Mapped[MixComplexity | None] = mapped_column(Enum(MixComplexity), nullable=True)
    mix_cost_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mix_bonus_master_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mix_bonus_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    kanekalon_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    kudri_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    materials_cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    amount_from_client: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    addons_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    addons_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    amortization_level: Mapped[AmortizationLevel | None] = mapped_column(Enum(AmortizationLevel), nullable=True)
    amortization_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    studio_fund_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_before_split: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    salon_cut_pct_at_time: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    salon_profit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    masters_pool: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    subcategory_key: Mapped[StudioOrderSubcategoryKey] = mapped_column(
        Enum(StudioOrderSubcategoryKey, native_enum=False, length=24),
        nullable=False,
    )
    rubber_length_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    rubber_blanks_on_elastic: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubber_weight_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    rubber_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    korrekciya_blanks_in_kit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    korrekciya_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    client: Mapped["Client"] = relationship()
    staff_rows: Mapped[list["StudioOrderStaff"]] = relationship(
        back_populates="studio_order", cascade="all, delete-orphan"
    )
    kit_usages: Mapped[list["StudioOrderKitUsage"]] = relationship(
        back_populates="studio_order", cascade="all, delete-orphan"
    )
    service_lines: Mapped[list["StudioOrderServiceLine"]] = relationship(
        back_populates="studio_order", cascade="all, delete-orphan"
    )


class StudioOrderStaff(Base):
    __tablename__ = "studio_order_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    studio_order_id: Mapped[int] = mapped_column(
        ForeignKey("studio_orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    percent: Mapped[float] = mapped_column(Float, nullable=False)

    studio_order: Mapped["StudioOrder"] = relationship(back_populates="staff_rows")
    user: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("studio_order_id", "user_id", name="uq_studio_order_staff_order_user"),)


class StudioOrderKitUsage(Base):
    __tablename__ = "studio_order_kit_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    studio_order_id: Mapped[int] = mapped_column(
        ForeignKey("studio_orders.id", ondelete="CASCADE"), nullable=False
    )
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id"), nullable=False)
    pieces_used: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    studio_order: Mapped["StudioOrder"] = relationship(back_populates="kit_usages")
    kit: Mapped["Kit"] = relationship()


class StudioOrderServiceLine(Base):
    __tablename__ = "studio_order_service_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    studio_order_id: Mapped[int] = mapped_column(
        ForeignKey("studio_orders.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    studio_order: Mapped["StudioOrder"] = relationship(back_populates="service_lines")
    service: Mapped["Service"] = relationship()


class Visit(Base):
    __tablename__ = "visits"

    # NOTE: All *_at_time fields are snapshots. Changing settings/material prices later
    # must not affect historical visits.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    client_type: Mapped[VisitClientType] = mapped_column(Enum(VisitClientType), nullable=False)
    price_type: Mapped[VisitPriceType] = mapped_column(Enum(VisitPriceType), nullable=False)
    # snapshot from client card at time of visit
    client_age_group: Mapped[ClientAgeGroup | None] = mapped_column(Enum(ClientAgeGroup), nullable=True)

    kanekalon_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    kudri_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mix_source: Mapped[MixSource | None] = mapped_column(Enum(MixSource), nullable=True)
    mix_complexity: Mapped[MixComplexity | None] = mapped_column(Enum(MixComplexity), nullable=True)
    mix_cost_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mix_bonus_master_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mix_bonus_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    kanekalon_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    kudri_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    materials_cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    amount_from_client: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Скидка от прайса, целые %; в расчёт себестоимости не входит, хранится для истории и подсказок.
    client_discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Addons reduce salon/master profit by rule (stored as a separate snapshot field).
    addons_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    addons_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    amortization_level: Mapped[AmortizationLevel | None] = mapped_column(Enum(AmortizationLevel), nullable=True)
    amortization_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    studio_fund_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_before_split: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    salon_cut_pct_at_time: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    salon_profit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    masters_pool: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    client: Mapped[Client] = relationship()
    masters: Mapped[list["VisitMaster"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    services: Mapped[list["VisitService"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    kit_usages: Mapped[list["VisitKitUsage"]] = relationship(back_populates="visit", cascade="all, delete-orphan")


class VisitAuditLog(Base):
    __tablename__ = "visit_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    visit: Mapped[Visit] = relationship()


class VisitMaster(Base):
    __tablename__ = "visit_masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"), nullable=False)
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    percent: Mapped[float] = mapped_column(Float, nullable=False)

    visit: Mapped[Visit] = relationship(back_populates="masters")
    master: Mapped[User] = relationship()

    __table_args__ = (UniqueConstraint("visit_id", "master_id", name="uq_visit_master"),)


class VisitService(Base):
    __tablename__ = "visit_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # snapshot names (for history)
    category_name: Mapped[str] = mapped_column(String(120), nullable=False)
    subcategory_name: Mapped[str] = mapped_column(String(160), nullable=False)
    service_name: Mapped[str] = mapped_column(String(200), nullable=False)

    visit: Mapped[Visit] = relationship(back_populates="services")
    service: Mapped[Service] = relationship()


class VisitKitUsage(Base):
    __tablename__ = "visit_kit_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"), nullable=False)
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id"), nullable=False)
    pieces_used: Mapped[int] = mapped_column(Integer, nullable=False)

    # snapshot: what we subtract from profit for this usage
    cost_amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    visit: Mapped[Visit] = relationship(back_populates="kit_usages")
    kit: Mapped[Kit] = relationship()
