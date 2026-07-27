"""Календарь: занятость мастеров и подписи услуг (фикс 33)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import json

from app.admin_service_catalog import _parse_estimated_duration
from app.calendar_occupancy import (
    COLOR_OCCUPANCY_ACTIVE,
    COLOR_OCCUPANCY_CONSULTATION,
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
    BookingPlannedServiceMaster,
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
    cat = ServiceCategory(name="Вся голова", short_name="Вся")
    sub = ServiceSubcategory(name="Косы", short_name="Кос", category=cat)
    svc = Service(name="Французская", short_name="Франц", subcategory=sub)
    assert format_service_catalog_path(svc) == "Вся голова → Косы → Французская"
    assert format_service_catalog_path(svc, prefer_short=True) == "Вся → Кос → Франц"


def test_format_service_catalog_path_partial_short_names() -> None:
    cat = ServiceCategory(name="Вся голова")
    sub = ServiceSubcategory(name="Вплетение комплекта", short_name="Вплет", category=cat)
    svc = Service(name="В 4 руки", subcategory=sub)
    assert format_service_catalog_path(svc, prefer_short=True) == "Вся голова → Вплет → В 4 руки"


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
    assert COLOR_OCCUPANCY_ACTIVE == "#69d186"
    assert COLOR_OCCUPANCY_PENDING == "#9CFF19"


def test_occupancy_color_for_consultation_always_yellow() -> None:
    from app.calendar_occupancy import occupancy_color_for_booking
    from app.db.models import BookingKind

    assert occupancy_color_for_booking(BookingStatus.ACTIVE, kind=BookingKind.CONSULTATION) == COLOR_OCCUPANCY_CONSULTATION
    assert (
        occupancy_color_for_booking(BookingStatus.PENDING_CONFIRMATION, kind=BookingKind.CONSULTATION)
        == COLOR_OCCUPANCY_CONSULTATION
    )
    assert occupancy_color_for_booking(BookingStatus.DONE, kind=BookingKind.CONSULTATION) == COLOR_OCCUPANCY_CONSULTATION
    assert occupancy_color_for_booking(BookingStatus.ACTIVE, kind=BookingKind.VISIT) == COLOR_OCCUPANCY_ACTIVE


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
    assert seg["client_name"] == "Клиент"
    assert seg["service_label"] == "Cat → Sub → Услуга"


def test_build_occupancy_uses_short_names(memory_db) -> None:
    db = memory_db
    db.add(Setting(key=DISPLAY_TIMEZONE, value="Europe/Moscow"))
    cat = ServiceCategory(name="Вся голова", short_name="Вся")
    sub = ServiceSubcategory(name="Вплетение комплекта", short_name="Вплет", category_id=None)
    db.add(cat)
    db.flush()
    sub.category_id = cat.id
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="В 4 руки",
        short_name="4руки",
        estimated_duration_minutes=120,
        is_active=True,
    )
    master = User(username="m5", password_hash="x", display_name="Мастер", role=UserRole.MASTER, is_active=True)
    admin = User(username="a5", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc, master, admin, client])
    db.flush()

    planned = datetime(2026, 5, 24, 7, 0, 0)
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

    day = date(2026, 5, 24)
    occ = build_occupancy_for_day(db, day=day, hour_from=9, hour_to=21, bookings=[booking])
    assert occ["segments"][0]["service_label"] == "Вся → Вплет → 4руки"


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


def test_build_occupancy_uses_individual_master_start(memory_db) -> None:
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
        estimated_duration_minutes=240,
        is_active=True,
    )
    m1 = User(username="m3", password_hash="x", display_name="Мастер 1", role=UserRole.MASTER, is_active=True)
    m2 = User(username="m4", password_hash="x", display_name="Мастер 2", role=UserRole.MASTER, is_active=True)
    admin = User(username="a3", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc, m1, m2, admin, client])
    db.flush()

    planned = datetime(2026, 5, 23, 7, 0, 0)  # 10:00 Moscow
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
    ps = BookingPlannedService(
        booking_id=booking.id,
        service_id=svc.id,
        sort_order=0,
        planned_start_time=planned,
        duration_minutes=240,
    )
    db.add(ps)
    db.flush()
    db.add(BookingPlannedServiceMaster(booking_planned_service_id=ps.id, master_id=m1.id))
    db.add(
        BookingPlannedServiceMaster(
            booking_planned_service_id=ps.id,
            master_id=m2.id,
            planned_start_time=datetime(2026, 5, 23, 8, 0, 0),  # 11:00 Moscow
        )
    )
    db.commit()

    db.refresh(booking)
    booking.planned_services = list(db.query(BookingPlannedService).filter_by(booking_id=booking.id).all())
    for row in booking.planned_services:
        row.service = svc
        row.masters = list(
            db.query(BookingPlannedServiceMaster).filter_by(booking_planned_service_id=row.id).all()
        )

    day = date(2026, 5, 23)
    occ = build_occupancy_for_day(db, day=day, hour_from=9, hour_to=21, bookings=[booking])
    segs = sorted(occ["segments"], key=lambda x: x["master_id"])
    assert len(segs) == 2
    assert segs[0]["master_id"] == m1.id and segs[0]["start_minutes"] == 10 * 60 and segs[0]["end_minutes"] == 14 * 60
    assert segs[1]["master_id"] == m2.id and segs[1]["start_minutes"] == 11 * 60 and segs[1]["end_minutes"] == 14 * 60


def test_build_occupancy_includes_consultation_booking(memory_db) -> None:
    db = memory_db
    db.add(Setting(key=DISPLAY_TIMEZONE, value="Europe/Moscow"))
    master = User(username="m_cons", password_hash="x", display_name="Мастер", role=UserRole.MASTER, is_active=True)
    admin = User(username="a_cons", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([master, admin, client])
    db.flush()

    # 16:15 Moscow = 13:15 UTC (naive stored as UTC in app)
    planned = datetime(2026, 7, 10, 13, 15, 0)
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=planned,
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.PENDING_CONFIRMATION,
        details_json=json.dumps({"consultation_duration_minutes": 45}, ensure_ascii=False),
    )
    db.add(booking)
    db.flush()
    db.add(BookingMaster(booking_id=booking.id, master_id=master.id))
    db.commit()
    db.refresh(booking)
    booking.masters = list(db.query(BookingMaster).filter_by(booking_id=booking.id).all())

    day = date(2026, 7, 10)
    occ = build_occupancy_for_day(db, day=day, hour_from=9, hour_to=21, bookings=[booking])
    segs = occ["segments"]
    assert len(segs) == 1
    seg = segs[0]
    assert seg["booking_id"] == booking.id
    assert seg["master_id"] == master.id
    assert seg["start_minutes"] == 16 * 60 + 15
    assert seg["end_minutes"] == 17 * 60
    assert seg["color"] == COLOR_OCCUPANCY_CONSULTATION
    assert seg["service_label"] == "Консультация"
    assert seg["kind"] == "CONSULTATION"


def test_build_occupancy_active_consultation_is_yellow(memory_db) -> None:
    db = memory_db
    db.add(Setting(key=DISPLAY_TIMEZONE, value="UTC"))
    master = User(username="m_c2", password_hash="x", display_name="Мастер", role=UserRole.MASTER, is_active=True)
    admin = User(username="a_c2", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([master, admin, client])
    db.flush()
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=datetime(2026, 7, 18, 10, 0, 0),
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.ACTIVE,
        details_json=json.dumps({"consultation_duration_minutes": 20}, ensure_ascii=False),
    )
    db.add(booking)
    db.flush()
    db.add(BookingMaster(booking_id=booking.id, master_id=master.id))
    db.commit()
    db.refresh(booking)
    booking.masters = list(db.query(BookingMaster).filter_by(booking_id=booking.id).all())

    occ = build_occupancy_for_day(db, day=date(2026, 7, 18), hour_from=9, hour_to=21, bookings=[booking])
    assert len(occ["segments"]) == 1
    assert occ["segments"][0]["color"] == COLOR_OCCUPANCY_CONSULTATION
    assert occ["segments"][0]["status"] == BookingStatus.ACTIVE.value
    assert occ["segments"][0]["kind"] == "CONSULTATION"
