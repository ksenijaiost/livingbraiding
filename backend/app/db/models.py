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
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UserRole(str, enum.Enum):
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
    FROM_STOCK = "FROM_STOCK"
    NO_MIX = "NO_MIX"
    SELF_MIXED = "SELF_MIXED"


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
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class StudioExpenseCategory(Base):
    __tablename__ = "studio_expense_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class StudioExpense(Base):
    __tablename__ = "studio_expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("studio_expense_categories.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)

    category: Mapped[StudioExpenseCategory | None] = relationship()


class ServiceCategory(Base):
    __tablename__ = "service_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ServiceSubcategory(Base):
    __tablename__ = "service_subcategories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("service_categories.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    category: Mapped[ServiceCategory] = relationship()

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_subcategory_per_category"),)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subcategory_id: Mapped[int] = mapped_column(ForeignKey("service_subcategories.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    price_junior_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_junior_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_middle_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_middle_to: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_senior_from: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_senior_to: Mapped[float | None] = mapped_column(Float, nullable=True)

    subcategory: Mapped[ServiceSubcategory] = relationship()

    __table_args__ = (UniqueConstraint("subcategory_id", "name", name="uq_service_per_subcategory"),)


class MaterialPriceCurrent(Base):
    __tablename__ = "material_prices_current"

    # This table stores current prices used as defaults for new visits.
    # Visits store snapshots (`*_price_per_gram_at_time`) and must never be recalculated.
    material_type: Mapped[MaterialType] = mapped_column(Enum(MaterialType), primary_key=True)
    price_per_gram: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Kit(Base):
    __tablename__ = "kits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class KitBatch(Base):
    __tablename__ = "kit_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kit_id: Mapped[int] = mapped_column(ForeignKey("kits.id"), nullable=False)
    pieces_total: Mapped[int] = mapped_column(Integer, nullable=False)
    pieces_available: Mapped[int] = mapped_column(Integer, nullable=False)

    # If taken from stock: subtract this value (pro-rated by pieces used) from visit profit.
    stock_price_total: Mapped[float | None] = mapped_column(Float, nullable=True)

    # If newly made: subtract this value (pro-rated by pieces used) from visit profit.
    cost_total: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    is_in_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    kit: Mapped[Kit] = relationship()


class Visit(Base):
    __tablename__ = "visits"

    # NOTE: All *_at_time fields are snapshots. Changing settings/material prices later
    # must not affect historical visits.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    performed_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False)
    client_type: Mapped[VisitClientType] = mapped_column(Enum(VisitClientType), nullable=False)
    price_type: Mapped[VisitPriceType] = mapped_column(Enum(VisitPriceType), nullable=False)
    client_age_group: Mapped[ClientAgeGroup | None] = mapped_column(Enum(ClientAgeGroup), nullable=True)
    client_source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    client_source_other: Mapped[str | None] = mapped_column(String(200), nullable=True)
    client_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    materials_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    kanekalon_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    kudri_grams: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    mix_source: Mapped[MixSource | None] = mapped_column(Enum(MixSource), nullable=True)

    kanekalon_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    kudri_price_per_gram_at_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    materials_cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    amount_from_client: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extra_cost_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    extra_cost_comment: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Addons reduce salon/master profit by rule (stored as a separate snapshot field).
    addons_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    addons_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    cost_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    profit_before_split: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    salon_cut_pct_at_time: Mapped[float] = mapped_column(Float, default=0.3, nullable=False)
    salon_profit: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    masters_pool: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    client: Mapped[Client] = relationship()
    masters: Mapped[list["VisitMaster"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    services: Mapped[list["VisitService"]] = relationship(back_populates="visit", cascade="all, delete-orphan")
    kit_usages: Mapped[list["VisitKitUsage"]] = relationship(back_populates="visit", cascade="all, delete-orphan")


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
    kit_batch_id: Mapped[int] = mapped_column(ForeignKey("kit_batches.id"), nullable=False)
    pieces_used: Mapped[int] = mapped_column(Integer, nullable=False)

    # snapshot: what we subtract from profit for this usage
    cost_amount: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    visit: Mapped[Visit] = relationship(back_populates="kit_usages")
    kit_batch: Mapped[KitBatch] = relationship()
