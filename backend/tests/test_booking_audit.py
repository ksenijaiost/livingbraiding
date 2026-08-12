"""Аудит брони: подписи полей и мастера по услугам."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.audit import FieldChange
from app.audit_field_labels import (
    audit_field_label,
    diff_planned_service_masters_audit,
    planned_service_masters_audit_field_label,
    resolve_audit_field_name,
    setting_key_audit_label,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    BookingStatus,
    Client,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    User,
    UserRole,
)
from app.routes.bookings import (
    _booking_masters_audit_changes,
    _collect_planned_service_masters_audit_lines,
)
from app.setting_keys import DISPLAY_TIMEZONE, SALON_CUT_PCT


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _visit_setup(db):
    db.add(Setting(key=DISPLAY_TIMEZONE, value="Europe/Moscow"))
    cat = ServiceCategory(name="Cat")
    sub = ServiceSubcategory(name="Sub", category_id=None)
    db.add(cat)
    db.flush()
    sub.category_id = cat.id
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Снятие", estimated_duration_minutes=120, is_active=True)
    admin = User(username="a1", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc, admin, client])
    db.flush()
    return svc, admin, client


def test_audit_field_label_maps_legacy_names() -> None:
    assert audit_field_label("visit_masters") == "Мастера (на весь визит)"
    assert audit_field_label("planned_services") == "Услуги в брони"
    assert audit_field_label("deposit_amount") == "Депозит"
    assert audit_field_label("pieces_available") == "Прядей доступно"


def test_setting_key_audit_label() -> None:
    assert setting_key_audit_label(SALON_CUT_PCT) == "Доля салона"
    assert resolve_audit_field_name(
        "value",
        log_table="setting_audit_logs",
        entity_id=SALON_CUT_PCT,
    ) == "Доля салона"


def test_audit_field_label_keeps_service_specific() -> None:
    label = "Мастера · Снятие (15:00)"
    assert audit_field_label(label) == label


def test_diff_planned_service_masters_audit_only_changed_line() -> None:
    before = [
        ("0|1|15:00", "Мастера · Снятие (15:00)", "Ира, Юля"),
        ("1|2|17:00", "Мастера · Наращивание (17:00)", "Ира, Юля"),
    ]
    after = [
        ("0|1|15:00", "Мастера · Снятие (15:00)", "Ира"),
        ("1|2|17:00", "Мастера · Наращивание (17:00)", "Ира, Юля"),
    ]
    changes = diff_planned_service_masters_audit(before, after)
    assert len(changes) == 1
    assert changes[0].field_name == "Мастера · Снятие (15:00)"
    assert changes[0].old_value == "Ира, Юля"
    assert changes[0].new_value == "Ира"


def test_planned_service_masters_audit_field_label_truncates_long_name() -> None:
    long_name = "У" * 100
    label = planned_service_masters_audit_field_label(long_name, "15:00")
    assert len(label) <= 120
    assert label.endswith("(15:00)")


def test_collect_planned_service_masters_audit_lines(memory_db) -> None:
    db = memory_db
    svc, admin, client = _visit_setup(db)

    master1 = User(username="m1", display_name="Ира", password_hash="x", role=UserRole.MASTER, is_active=True)
    master2 = User(username="m2", display_name="Юля", password_hash="x", role=UserRole.MASTER, is_active=True)
    db.add_all([master1, master2])
    db.flush()

    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=datetime(2026, 8, 11, 8, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        planned_service_id=svc.id,
    )
    db.add(booking)
    db.flush()
    ps = BookingPlannedService(
        booking_id=booking.id,
        service_id=svc.id,
        sort_order=0,
        planned_start_time=datetime(2026, 8, 11, 8, 0),
        duration_minutes=120,
    )
    db.add(ps)
    db.flush()
    db.add_all(
        [
            BookingPlannedServiceMaster(booking_planned_service_id=ps.id, master_id=master1.id),
            BookingPlannedServiceMaster(booking_planned_service_id=ps.id, master_id=master2.id),
        ]
    )
    db.commit()

    lines = _collect_planned_service_masters_audit_lines(db, booking.id)
    assert len(lines) == 1
    assert lines[0][2] == "Ира, Юля"


def test_booking_masters_audit_changes_all_mode() -> None:
    changes = _booking_masters_audit_changes(
        kind_raw=BookingKind.VISIT.value,
        masters_mode="all",
        before_visit_masters="Ира",
        after_visit_masters="Ира, Юля",
        before_sale_staff="—",
        after_sale_staff="—",
        before_service_lines=[],
        after_service_lines=[],
    )
    assert len(changes) == 1
    assert changes[0].field_name == "Мастера (на весь визит)"


def test_booking_masters_audit_changes_per_service_mode() -> None:
    before = [("0|1|15:00", "Мастера · Снятие (15:00)", "Ира, Юля")]
    after = [("0|1|15:00", "Мастера · Снятие (15:00)", "Ира")]
    changes = _booking_masters_audit_changes(
        kind_raw=BookingKind.VISIT.value,
        masters_mode="per_service",
        before_visit_masters="Ира, Юля",
        after_visit_masters="Ира",
        before_sale_staff="—",
        after_sale_staff="—",
        before_service_lines=before,
        after_service_lines=after,
    )
    assert len(changes) == 1
    assert "Снятие" in changes[0].field_name
