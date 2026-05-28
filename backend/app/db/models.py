from __future__ import annotations

"""
SQLAlchemy ORM models.

Key principle: **no historical recalculation**.

Anything that can change over time (prices, salon %, etc.) must be stored as a *snapshot*
inside the visit-related tables, so changing settings only affects *future* visits.
"""

import enum
from datetime import datetime, date, time

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UserRole(str, enum.Enum):
    ADMIN_SUPER = "ADMIN_SUPER"
    ADMIN = "ADMIN"
    MASTER = "MASTER"
    TECHSPEC = "TECHSPEC"


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
    """Сложность смешки; ₽/г задаются в настройках (work_rates)."""

    LIGHT = "LIGHT"  # 0.5 по умолчанию
    STANDARD = "STANDARD"  # 1
    KANEK = "KANEK"  # 1.5
    THERMO = "THERMO"  # 2
    LENGTH = "LENGTH"  # 2.5


class AmortizationLevel(str, enum.Enum):
    MIN = "MIN"  # 100 ₽
    MID = "MID"  # 200 ₽
    MAX = "MAX"  # 500 ₽


class KitBlanksCondition(str, enum.Enum):
    """Состояние заготовок в комплекте (карточка склада)."""

    NEW = "NEW"  # только новые
    USED = "USED"  # только Б/У
    MIXED = "MIXED"  # новые и Б/У (50/50 в смысле «смешанный набор»)


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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("phone", name="uq_users_phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    master_level: Mapped[MasterLevel | None] = mapped_column(Enum(MasterLevel), nullable=True)
    # Нормализованный номер (только цифры, ≥10), для входа вместо логина; уникален среди непустых.
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    role_assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="UserRoleAssignment.id",
    )


class UserRoleAssignment(Base):
    """Назначенные роли (активная роль выбирается в сессии, см. cookie)."""

    __tablename__ = "user_role_assignments"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_role_assignments_user_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False)

    user: Mapped["User"] = relationship(back_populates="role_assignments")


class UserAuditLog(Base):
    """История изменений карточки сотрудника (суперадмин)."""

    __tablename__ = "user_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    photo_1: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_2: Mapped[str | None] = mapped_column(String(300), nullable=True)
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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])

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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
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
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    voided_by_user: Mapped["User | None"] = relationship(foreign_keys=[voided_by_user_id])
    subcategory: Mapped["StudioExpenseSubcategory"] = relationship()


class ServiceCategory(Base):
    __tablename__ = "service_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    questionnaire_fields: Mapped[list["CategoryQuestionnaireField"]] = relationship(
        back_populates="category",
        order_by="CategoryQuestionnaireField.sort_order",
    )

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])


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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    category: Mapped[ServiceCategory] = relationship(back_populates="questionnaire_fields")
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])

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
    # Форма визита: блок «Хвост/резинка» (склад) для услуг этой подкатегории, если у услуги нет своего override.
    show_tail_section: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Показывать поле «Описание про материал» из анкеты категории (если оно задано на категории).
    show_material_description: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Расширенный блок термозамещения на шаге 2 визита + шаблоны клиента (без общих полей категории).
    show_thermo_visit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    category: Mapped[ServiceCategory] = relationship()
    questionnaire_fields: Mapped[list[SubcategoryQuestionnaireField]] = relationship(
        back_populates="subcategory",
        order_by="SubcategoryQuestionnaireField.sort_order",
    )

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_subcategory_per_category"),)

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(ForeignKey("service_subcategories.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    estimated_duration_minutes: Mapped[int] = mapped_column(Integer, default=120, nullable=False)

    # Прайс по уровням мастера (в UI: младший / мастер / старший); любой диапазон может быть NULL.
    price_junior_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_junior_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_middle_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_middle_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_senior_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_senior_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    # None — брать из подкатегории show_kit_section; True/False — принудительно.
    kit_section_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # None — брать из подкатегории show_tail_section; True/False — принудительно.
    tail_section_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # None — брать из подкатегории show_material_description; True — показывать; False — не показывать.
    material_description_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Розница «Продажа материала»: какие блоки ценообразования показывать (независимые флаги).
    retail_material_kanekalon: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retail_material_kudri: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retail_material_mix: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    subcategory: Mapped[ServiceSubcategory] = relationship()
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    subcategory: Mapped[ServiceSubcategory] = relationship(back_populates="questionnaire_fields")
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])

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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    service: Mapped[Service] = relationship(back_populates="questionnaire_fields")
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])

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
    blanks_condition: Mapped[KitBlanksCondition] = mapped_column(
        Enum(KitBlanksCondition, native_enum=False, length=16),
        nullable=False,
        default=KitBlanksCondition.NEW,
    )
    photo_1: Mapped[str | None] = mapped_column(String(300), nullable=True)
    weight_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    length_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    materials_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    color_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Из наличия: вычитаемая из прибыли визита цена (пропорционально списанным заготовкам).
    stock_price_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Снимок состава комплекта (для расчёта цены по прайсу «Заказ»): list[{key, qty}] или dict key->qty.
    composition_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Текстовый снимок расчёта цены комплекта (построчно: вид, кол-во, цена за шт, сумма, доп. расходы).
    stock_price_snapshot_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Скидка с цены комплекта, целые проценты 0–100; рубли = цена × (процент/100) при списании.
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Себестоимость всего комплекта: затраты + ЗП авторов (едино поле; author_cost_total не используем в расчётах).
    cost_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Текстовый снимок расчёта себестоимости (ЗП по видам, материал, смешка, доп. расходы).
    cost_snapshot_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Устарело: не участвует в формуле фонда студии (себестоимость включает ЗП авторов).
    author_cost_total: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])

    # Автор(ы) комплекта: сотрудники студии и/или отметка «Извне» (заполняется при внесении карточки).
    author_external: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    author_staff_links: Mapped[list["KitAuthorStaff"]] = relationship(
        back_populates="kit",
        cascade="all, delete-orphan",
        order_by="KitAuthorStaff.sort_order",
    )
    reserves: Mapped[list["KitReserve"]] = relationship(
        back_populates="kit",
        cascade="all, delete-orphan",
        order_by="KitReserve.id",
    )
    blank_stock_lines: Mapped[list["KitBlankStock"]] = relationship(
        back_populates="kit",
        cascade="all, delete-orphan",
        order_by="KitBlankStock.kit_key",
    )

    @property
    def is_reserved(self) -> bool:
        return bool(self.reserves)


class KitBlankStock(Base):
    """Остаток заготовок по ключу состава (composition_json / kit_key в прайсе)."""

    __tablename__ = "kit_blank_stock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id", ondelete="CASCADE"), nullable=False)
    kit_key: Mapped[str] = mapped_column(String(80), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    kit: Mapped["Kit"] = relationship(back_populates="blank_stock_lines")

    __table_args__ = (UniqueConstraint("kit_id", "kit_key", name="uq_kit_blank_stock_kit_key"),)


class KitReserve(Base):
    """Резерв заготовок по комплекту: несколько строк на один kit (лимит — настройка)."""

    __tablename__ = "kit_reserves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kit_id: Mapped[int] = mapped_column(Integer, ForeignKey("kits.id", ondelete="CASCADE"), nullable=False)
    pieces_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    reserved_by_user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    reserved_for_client_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("clients.id"), nullable=True)
    reserved_for_user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    booking_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bookings.id", ondelete="SET NULL"), nullable=True)
    # Ключ состава; NULL — старый резерв «без разбивки по видам» (только scalar pieces_available).
    kit_key: Mapped[str | None] = mapped_column(String(80), nullable=True)

    kit: Mapped["Kit"] = relationship(back_populates="reserves")
    reserved_by_user: Mapped["User"] = relationship(foreign_keys=[reserved_by_user_id])
    reserved_for_client: Mapped["Client | None"] = relationship(foreign_keys=[reserved_for_client_id])
    reserved_for_user: Mapped["User | None"] = relationship(foreign_keys=[reserved_for_user_id])
    booking: Mapped["Booking | None"] = relationship(back_populates="kit_reserves")


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


class VisitMastersScope(str, enum.Enum):
    VISIT = "VISIT"
    PER_SERVICE = "PER_SERVICE"


class BookingKind(str, enum.Enum):
    VISIT = "VISIT"
    PRODUCT_SALE = "PRODUCT_SALE"


class BookingStatus(str, enum.Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    ACTIVE = "ACTIVE"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class Booking(Base):
    """Бронь для будущего визита/продажи (заполняется админом)."""

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    planned_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    kind: Mapped[BookingKind] = mapped_column(
        Enum(BookingKind, native_enum=False, length=20),
        nullable=False,
    )
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, native_enum=False, length=24),
        nullable=False,
        default=BookingStatus.PENDING_CONFIRMATION,
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    quoted_price_text: Mapped[str | None] = mapped_column(String(120), nullable=True)
    deposit_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    photo_1: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_2: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_3: Mapped[str | None] = mapped_column(String(300), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # VISIT planning
    planned_service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"), nullable=True)

    # PRODUCT_SALE planning: строковое значение из ProductSaleKind (валидируем в обработчиках формы).
    planned_product_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)

    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    consultation_id: Mapped[int | None] = mapped_column(
        ForeignKey("consultations.id"),
        nullable=True,
        unique=True,
    )

    masters_scope: Mapped[VisitMastersScope] = mapped_column(
        Enum(VisitMastersScope, native_enum=False, length=16),
        nullable=False,
        default=VisitMastersScope.VISIT,
    )
    same_master_shares_all_services: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    cancelled_by_user: Mapped["User | None"] = relationship(foreign_keys=[cancelled_by_user_id])
    client: Mapped["Client"] = relationship()
    planned_service: Mapped["Service | None"] = relationship(foreign_keys=[planned_service_id])
    masters: Mapped[list["BookingMaster"]] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
    )
    kit_reserves: Mapped[list["KitReserve"]] = relationship(back_populates="booking")
    consultation: Mapped["Consultation | None"] = relationship(
        back_populates="booking",
        foreign_keys=[consultation_id],
    )
    planned_services: Mapped[list["BookingPlannedService"]] = relationship(
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingPlannedService.sort_order",
    )


class BookingPlannedService(Base):
    __tablename__ = "booking_planned_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    planned_start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    booking: Mapped["Booking"] = relationship(back_populates="planned_services")
    service: Mapped["Service"] = relationship()
    masters: Mapped[list["BookingPlannedServiceMaster"]] = relationship(
        back_populates="planned_service",
        cascade="all, delete-orphan",
    )


class BookingPlannedServiceMaster(Base):
    __tablename__ = "booking_planned_service_masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_planned_service_id: Mapped[int] = mapped_column(
        ForeignKey("booking_planned_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    planned_service: Mapped["BookingPlannedService"] = relationship(back_populates="masters")
    master: Mapped["User"] = relationship()

    __table_args__ = (
        UniqueConstraint("booking_planned_service_id", "master_id", name="uq_booking_planned_service_master"),
    )


class BookingMaster(Base):
    """Мастера, которых планируют на бронь визита (без долей)."""

    __tablename__ = "booking_masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False)
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    booking: Mapped["Booking"] = relationship(back_populates="masters")
    master: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("booking_id", "master_id", name="uq_booking_master"),)


class BookingStaffKind(str, enum.Enum):
    """Назначенные мастера для броней (не только визит)."""

    SALE_KIT_ORDER = "SALE_KIT_ORDER"  # комплект на заказ (может быть несколько)
    SALE_RUBBER_ORDER = "SALE_RUBBER_ORDER"  # хвост/резинка на заказ (ровно один)


class BookingStaff(Base):
    """Сотрудники, назначенные на бронь (используется для отображения в «Мои записи»)."""

    __tablename__ = "booking_staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[BookingStaffKind] = mapped_column(
        Enum(BookingStaffKind, native_enum=False, length=24),
        nullable=False,
    )

    booking: Mapped["Booking"] = relationship()
    user: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("booking_id", "user_id", "kind", name="uq_booking_staff"),)


class BookingAuditLog(Base):
    __tablename__ = "booking_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    booking_id: Mapped[int] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    booking: Mapped["Booking"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class MasterScheduleStatus(str, enum.Enum):
    WORKING = "WORKING"
    DAY_OFF = "DAY_OFF"


class MasterScheduleDay(Base):
    """График работы мастера по дням (рабочий/выходной + интервал и перерыв)."""

    __tablename__ = "master_schedule_days"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[MasterScheduleStatus] = mapped_column(
        Enum(MasterScheduleStatus, native_enum=False, length=16), nullable=False
    )
    # Если status=WORKING, то интервалы могут быть NULL → подставляются часы по умолчанию в доменной логике.
    time_from: Mapped[time | None] = mapped_column(Time, nullable=True)
    time_to: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_from: Mapped[time | None] = mapped_column(Time, nullable=True)
    break_to: Mapped[time | None] = mapped_column(Time, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    master: Mapped["User"] = relationship(foreign_keys=[master_id])

    __table_args__ = (
        UniqueConstraint("master_id", "work_date", name="uq_master_schedule_day"),
    )


class MasterScheduleAuditLog(Base):
    """Audit changes for master schedule days (поля old/new)."""

    __tablename__ = "master_schedule_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)

    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])
    master: Mapped["User"] = relationship(foreign_keys=[master_id])


class Consultation(Base):
    """Консультация мастера (бесплатно для клиента), может перейти в бронь."""

    __tablename__ = "consultations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    consultation_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    types_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    preliminary_cost_text: Mapped[str | None] = mapped_column(String(120), nullable=True)
    photo_1: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_2: Mapped[str | None] = mapped_column(String(300), nullable=True)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    client: Mapped["Client"] = relationship()
    service: Mapped["Service | None"] = relationship(foreign_keys=[service_id])
    booking: Mapped["Booking | None"] = relationship(
        back_populates="consultation",
        foreign_keys="Booking.consultation_id",
        uselist=False,
    )
    planned_services: Mapped[list["ConsultationService"]] = relationship(
        back_populates="consultation",
        cascade="all, delete-orphan",
        order_by="ConsultationService.sort_order",
    )


class ConsultationService(Base):
    __tablename__ = "consultation_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consultation_id: Mapped[int] = mapped_column(
        ForeignKey("consultations.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    consultation: Mapped["Consultation"] = relationship(back_populates="planned_services")
    service: Mapped["Service"] = relationship()

    __table_args__ = (UniqueConstraint("consultation_id", "service_id", name="uq_consultation_service"),)


class ConsultationAuditLog(Base):
    __tablename__ = "consultation_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consultation_id: Mapped[int] = mapped_column(
        ForeignKey("consultations.id", ondelete="CASCADE"),
        nullable=False,
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    consultation: Mapped["Consultation"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    amount_from_client: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)

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
    material_kanekalon_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_kudri_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_kanekalon_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_kudri_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_manual_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_mix_source: Mapped[MixSource | None] = mapped_column(Enum(MixSource), nullable=True)
    material_mix_complexity: Mapped[MixComplexity | None] = mapped_column(Enum(MixComplexity), nullable=True)
    material_mix_cost_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    material_mix_bonus_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    material_mix_bonus_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    material_mix_standalone_grams: Mapped[float | None] = mapped_column(Float, nullable=True)
    material_cost_review_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # KIT
    kit_id: Mapped[int | None] = mapped_column(ForeignKey("kits.id"), nullable=True)
    kit_pieces_sold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kit_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    kit_lines_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # RUBBER
    rubber_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubber_price_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # OTHER
    other_description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Маржа в фонд студии (снимок для розницы; проводки ЗП по этому полю).
    studio_margin_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    voided_by_user: Mapped["User | None"] = relationship(foreign_keys=[voided_by_user_id])
    client: Mapped["Client"] = relationship()
    booking: Mapped["Booking | None"] = relationship(foreign_keys=[booking_id])
    material_service: Mapped["Service | None"] = relationship(foreign_keys=[material_service_id])
    material_mix_bonus_user: Mapped["User | None"] = relationship(foreign_keys=[material_mix_bonus_user_id])
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
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Дата события (для календаря/периодов). Для старых записей может быть NULL → используем created_at.
    performed_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_voided: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    voided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_kit_id: Mapped[int | None] = mapped_column(ForeignKey("kits.id"), nullable=True)

    kind: Mapped[WorkKind] = mapped_column(
        Enum(WorkKind, native_enum=False, length=32),
        nullable=False,
    )
    scope: Mapped[WorkScope] = mapped_column(
        Enum(WorkScope, native_enum=False, length=24),
        nullable=False,
    )

    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    amount_from_client: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    voided_by_user: Mapped["User | None"] = relationship(foreign_keys=[voided_by_user_id])
    booking: Mapped["Booking | None"] = relationship(foreign_keys=[booking_id])
    client: Mapped["Client | None"] = relationship()
    created_kit: Mapped["Kit | None"] = relationship(foreign_keys=[created_kit_id])
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


class PayrollFundEntryKind(str, enum.Enum):
    ACCRUAL = "ACCRUAL"
    STORNO = "STORNO"
    PAYOUT = "PAYOUT"
    EXPENSE = "EXPENSE"


class PayrollFundSide(str, enum.Enum):
    MASTER = "MASTER"
    STUDIO = "STUDIO"


class PayrollFundSourceKind(str, enum.Enum):
    VISIT = "VISIT"
    VISIT_SERVICE = "VISIT_SERVICE"
    WORK = "WORK"
    PRODUCT_SALE = "PRODUCT_SALE"
    CONSULTATION = "CONSULTATION"
    STUDIO_EXPENSE = "STUDIO_EXPENSE"
    MANUAL = "MANUAL"


class PayrollFundPayoutPaymentKind(str, enum.Enum):
    """Способ фактической выплаты (только для проводок PAYOUT)."""

    UNSPECIFIED = "UNSPECIFIED"
    NON_CASH = "NON_CASH"
    CASH = "CASH"


class PayrollFundLedger(Base):
    """Журнал фондов ЗП: + начисление, − сторно/выплата (сальдо = сумма amount).

    Для PAYOUT: при side=MASTER user_id — сотрудник, с чьего фонда списание; при side=STUDIO —
    сотрудник-получатель (фонд студии уменьшается на сумму выплаты ему).
    """

    __tablename__ = "payroll_fund_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    entry_kind: Mapped[PayrollFundEntryKind] = mapped_column(
        Enum(PayrollFundEntryKind, native_enum=False, length=20),
        nullable=False,
    )
    side: Mapped[PayrollFundSide] = mapped_column(
        Enum(PayrollFundSide, native_enum=False, length=16),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    source_kind: Mapped[PayrollFundSourceKind] = mapped_column(
        Enum(PayrollFundSourceKind, native_enum=False, length=24),
        nullable=False,
    )
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    storno_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("payroll_fund_ledger.id", ondelete="SET NULL"),
        nullable=True,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    payout_payment_kind: Mapped[PayrollFundPayoutPaymentKind | None] = mapped_column(
        Enum(PayrollFundPayoutPaymentKind, native_enum=False, length=20),
        nullable=True,
    )

    created_by_user: Mapped["User | None"] = relationship(foreign_keys=[created_by_user_id])
    user: Mapped["User | None"] = relationship(foreign_keys=[user_id])


class Visit(Base):
    __tablename__ = "visits"

    # NOTE: All *_at_time fields are snapshots. Changing settings/material prices later
    # must not affect historical visits.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)
    # Основной комплект «из наличия»: уже оплачен (напр. при брони) — не входит в себестоимость визита.
    kit_paid_separately: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    photo_1: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_2: Mapped[str | None] = mapped_column(String(300), nullable=True)
    photo_3: Mapped[str | None] = mapped_column(String(300), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Addons reduce salon/master profit by rule (stored as a separate snapshot field).
    addons_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    addons_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    amortization_level: Mapped[AmortizationLevel | None] = mapped_column(Enum(AmortizationLevel), nullable=True)
    amortization_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    studio_fund_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_before_split: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    salon_cut_pct_at_time: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    salon_profit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    masters_pool: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    masters_scope: Mapped[VisitMastersScope] = mapped_column(
        Enum(VisitMastersScope, native_enum=False, length=16),
        nullable=False,
        default=VisitMastersScope.VISIT,
    )
    same_master_shares_all_services: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    client: Mapped[Client] = relationship()
    booking: Mapped["Booking | None"] = relationship(foreign_keys=[booking_id])
    created_by_user: Mapped["User | None"] = relationship(foreign_keys=[created_by_user_id])
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    cancelled_by_user: Mapped["User | None"] = relationship(foreign_keys=[cancelled_by_user_id])
    masters: Mapped[list["VisitMaster"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    services: Mapped[list["VisitService"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    kit_usages: Mapped[list["VisitKitUsage"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    draft_source: Mapped["VisitDraft | None"] = relationship(
        back_populates="finalized_visit",
        foreign_keys="VisitDraft.finalized_visit_id",
        uselist=False,
    )


class VisitDraft(Base):
    __tablename__ = "visit_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"), nullable=True)

    form_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    locked_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_visit_id: Mapped[int | None] = mapped_column(ForeignKey("visits.id"), nullable=True)

    client: Mapped[Client] = relationship()
    booking: Mapped["Booking | None"] = relationship()
    created_by_user: Mapped["User"] = relationship(foreign_keys=[created_by_user_id])
    updated_by_user: Mapped["User | None"] = relationship(foreign_keys=[updated_by_user_id])
    locked_by_user: Mapped["User | None"] = relationship(foreign_keys=[locked_by_user_id])
    finalized_visit: Mapped["Visit | None"] = relationship(
        back_populates="draft_source",
        foreign_keys=[finalized_visit_id],
    )
    participants: Mapped[list["VisitDraftParticipant"]] = relationship(
        back_populates="visit_draft",
        cascade="all, delete-orphan",
    )


class VisitDraftParticipant(Base):
    __tablename__ = "visit_draft_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_draft_id: Mapped[int] = mapped_column(
        ForeignKey("visit_drafts.id", ondelete="CASCADE"),
        nullable=False,
    )
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    visit_draft: Mapped["VisitDraft"] = relationship(back_populates="participants")
    master: Mapped["User"] = relationship()


class VisitAuditLog(Base):
    __tablename__ = "visit_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    visit: Mapped[Visit] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class ClientAuditLog(Base):
    __tablename__ = "client_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    client: Mapped["Client"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class KitAuditLog(Base):
    __tablename__ = "kit_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id", ondelete="CASCADE"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    kit: Mapped["Kit"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class ProductSaleAuditLog(Base):
    __tablename__ = "product_sale_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("product_sales.id", ondelete="CASCADE"), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    sale: Mapped["ProductSale"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class StudioExpenseAuditLog(Base):
    __tablename__ = "studio_expense_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expense_id: Mapped[int] = mapped_column(
        ForeignKey("studio_expenses.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    expense: Mapped["StudioExpense"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class WorkForInventoryAuditLog(Base):
    __tablename__ = "work_for_inventory_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("work_for_inventory.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    work: Mapped["WorkForInventory"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class SettingAuditLog(Base):
    __tablename__ = "setting_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    setting_key: Mapped[str] = mapped_column(
        ForeignKey("settings.key", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    setting: Mapped["Setting"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class WorkRateAuditLog(Base):
    __tablename__ = "work_rate_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_rate_id: Mapped[int] = mapped_column(
        ForeignKey("work_rates.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    work_rate: Mapped["WorkRate"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class ServiceCategoryAuditLog(Base):
    __tablename__ = "service_category_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("service_categories.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped["ServiceCategory"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class ServiceSubcategoryAuditLog(Base):
    __tablename__ = "service_subcategory_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(
        ForeignKey("service_subcategories.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    subcategory: Mapped["ServiceSubcategory"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class ServiceAuditLog(Base):
    __tablename__ = "service_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    service: Mapped["Service"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class CategoryQuestionnaireFieldAuditLog(Base):
    __tablename__ = "category_questionnaire_field_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("category_questionnaire_fields.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    field: Mapped["CategoryQuestionnaireField"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class SubcategoryQuestionnaireFieldAuditLog(Base):
    __tablename__ = "subcategory_questionnaire_field_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("subcategory_questionnaire_fields.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    field: Mapped["SubcategoryQuestionnaireField"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


class ServiceQuestionnaireFieldAuditLog(Base):
    __tablename__ = "service_questionnaire_field_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("service_questionnaire_fields.id", ondelete="CASCADE"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    field_name: Mapped[str] = mapped_column(String(120), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    field: Mapped["ServiceQuestionnaireField"] = relationship()
    changed_by_user: Mapped["User | None"] = relationship(foreign_keys=[changed_by_user_id])


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

    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    amount_from_client: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    client_discount_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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
    addons_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    addons_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    amortization_level: Mapped[AmortizationLevel | None] = mapped_column(Enum(AmortizationLevel), nullable=True)
    amortization_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    studio_fund_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_before_split: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    salon_cut_pct_at_time: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    salon_profit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    masters_pool: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    kit_paid_separately: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    visit: Mapped[Visit] = relationship(back_populates="services")
    service: Mapped[Service] = relationship()
    cancelled_by_user: Mapped["User | None"] = relationship(foreign_keys=[cancelled_by_user_id])
    masters: Mapped[list["VisitServiceMaster"]] = relationship(
        back_populates="visit_service",
        cascade="all, delete-orphan",
    )
    kit_usages: Mapped[list["VisitKitUsage"]] = relationship(back_populates="visit_service")


class VisitServiceMaster(Base):
    __tablename__ = "visit_service_masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_service_id: Mapped[int] = mapped_column(
        ForeignKey("visit_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    master_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    percent: Mapped[float] = mapped_column(Float, nullable=False)

    visit_service: Mapped["VisitService"] = relationship(back_populates="masters")
    master: Mapped["User"] = relationship()

    __table_args__ = (UniqueConstraint("visit_service_id", "master_id", name="uq_visit_service_master"),)


class VisitKitUsage(Base):
    __tablename__ = "visit_kit_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey("visits.id"), nullable=False)
    visit_service_id: Mapped[int | None] = mapped_column(ForeignKey("visit_services.id"), nullable=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id"), nullable=False)
    pieces_used: Mapped[int] = mapped_column(Integer, nullable=False)

    # snapshot: what we subtract from profit for this usage
    cost_amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    usage_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    visit: Mapped[Visit] = relationship(back_populates="kit_usages")
    visit_service: Mapped["VisitService | None"] = relationship(back_populates="kit_usages")
    kit: Mapped[Kit] = relationship()
