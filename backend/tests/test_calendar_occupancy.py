"""Календарь: занятость мастеров и подписи услуг (фикс 33)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_service_catalog import _parse_estimated_duration
from app.calendar_occupancy import (
    COLOR_OCCUPANCY_ACTIVE,
    COLOR_OCCUPANCY_PENDING,
    build_occupancy_for_day,
    occupancy_color_for_status,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingPlannedService,
    BookingStatus,
    Client,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    User,
    UserRole,
    VisitService,
)
from app.setting_keys import DISPLAY_TIMEZONE
from app.ui_service_display import (
    format_service_catalog_path,
    format_visit_service_catalog_path,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_format_service_catalog_path() -> None:
    cat = ServiceCategory(name="Вся голова")
    sub = ServiceSubcategory(name="Косы", category=cat)
    svc = Service(name="Французская", subcategory=sub)
    assert format_service_catalog_path(svc) == "Вся голова → Косы → Французская"


def test_format_visit_service_catalog_path() -> None:
    vs = VisitService(
        category_name="Вся голова",
        subcategory_name="Косы",
        service_name="Французская",
    )
    assert format_visit_service_catalog_path(vs) == "Вся голова → Косы → Французская"


def test_occupancy_color_for_status() -> None:
    assert occupancy_color_for_status(BookingStatus.ACTIVE) == COLOR_OCCUPANCY_ACTIVE
    assert occupancy_color_for_status(BookingStatus.PENDING_CONFIRMATION) == COLOR_OCCUPANCY_PENDING
    assert occupancy_color_for_status(BookingStatus.DONE) == COLOR_OCCUPANCY_ACTIVE


def test_parse_estimated_duration() -> None:
    assert _parse_estimated_duration("120") == 120
    with pytest.raises(ValueError):
        _parse_estimated_duration("")
    with pytest.raises(ValueError):
        _parse_estimated_duration("0")
    with pytest.raises(ValueError):
        _parse_estimated_duration("2000")


def test_build_occupancy_segments_moscow(memory_db) -> None:
    db = memory_db
    db.add(Setting(key=DISPLAY_TIMEZONE, value="Europe/Moscow"))
    cat = ServiceCategory(name="Cat")
    sub = ServiceSubcategory(name="Sub", category_id=None)
    db.add(cat)
    db.flush()
    sub.category_id = cat.id
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Услуга",
        estimated_duration_minutes=120,
        is_active=True,
    )
    master = User(username="m1", password_hash="x", display_name="Мастер", role=UserRole.MASTER, is_active=True)
    admin = User(username="a1", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc, master, admin, client])
    db.flush()

    # 10:00 Moscow = 07:00 UTC (naive stored as UTC in app)
    planned = datetime(2026, 5, 23, 7, 0, 0)
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=planned,
        planned_service_id=svc.id,
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
    )
    db.add(booking)
    db.flush()
    db.add(BookingMaster(booking_id=booking.id, master_id=master.id))
    db.commit()
    db.refresh(booking)
    db.refresh(svc)
    sub.category = cat
    svc.subcategory = sub
    booking.planned_service = svc
    booking.masters = list(db.query(BookingMaster).filter_by(booking_id=booking.id).all())

    day = date(2026, 5, 23)
    occ = build_occupancy_for_day(db, day=day, hour_from=9, hour_to=21, bookings=[booking])
    segs = occ["segments"]
    assert len(segs) == 1
    seg = segs[0]
    assert seg["booking_id"] == booking.id
    assert seg["master_id"] == master.id
    assert seg["start_minutes"] == 10 * 60
    assert seg["end_minutes"] == 12 * 60
    assert seg["color"] == COLOR_OCCUPANCY_ACTIVE


def test_build_occupancy_uses_planned_service_duration_override(memory_db) -> None:
    db = memory_db
    db.add(Setting(key=DISPLAY_TIMEZONE, value="Europe/Moscow"))
    cat = ServiceCategory(name="Cat")
    sub = ServiceSubcategory(name="Sub", category_id=None)
    db.add(cat)
    db.flush()
    sub.category_id = cat.id
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Услуга",
        estimated_duration_minutes=120,
        is_active=True,
    )
    master = User(username="m2", password_hash="x", display_name="Мастер", role=UserRole.MASTER, is_active=True)
    admin = User(username="a2", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc, master, admin, client])
    db.flush()

    planned = datetime(2026, 5, 23, 7, 0, 0)
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=planned,
        planned_service_id=svc.id,
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
    )
    db.add(booking)
    db.flush()
    db.add(BookingMaster(booking_id=booking.id, master_id=master.id))
    db.add(
        BookingPlannedService(
            booking_id=booking.id,
            service_id=svc.id,
            sort_order=0,
            planned_start_time=planned,
            duration_minutes=240,
        )
    )
    db.commit()
    db.refresh(booking)
    sub.category = cat
    svc.subcategory = sub
    booking.planned_service = svc
    booking.masters = list(db.query(BookingMaster).filter_by(booking_id=booking.id).all())
    booking.planned_services = list(db.query(BookingPlannedService).filter_by(booking_id=booking.id).all())
    for ps in booking.planned_services:
        ps.service = svc

    day = date(2026, 5, 23)
    occ = build_occupancy_for_day(db, day=day, hour_from=9, hour_to=21, bookings=[booking])
    segs = occ["segments"]
    assert len(segs) == 1
    assert segs[0]["start_minutes"] == 10 * 60
    assert segs[0]["end_minutes"] == 14 * 60
