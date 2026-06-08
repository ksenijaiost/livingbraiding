"""Бронь визита: несколько услуг, длительность, синхронизация planned_services."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingPlannedService,
    BookingStatus,
    Client,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    User,
    UserRole,
)
from app.kit_inlay_visit import service_requires_kit_block
from app.routes.bookings import (
    _apply_parsed_visit_lines_to_fp,
    _booking_details_from_form,
    _collect_line_service_kits_from_fp,
    _expand_line_service_kits_to_fp,
    _parse_booking_visit_lines_and_masters,
    _planned_services_audit_label,
    _sync_booking_planned_services_with_lines,
    _validate_line_service_kits_for_reserve,
    _validate_visit_stock_kit_for_reserve,
    _visit_kit_mode_is_in_stock,
)
from app.setting_keys import DISPLAY_TIMEZONE


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
    svc1 = Service(subcategory_id=sub.id, name="Услуга 1", estimated_duration_minutes=120, is_active=True)
    svc2 = Service(subcategory_id=sub.id, name="Услуга 2", estimated_duration_minutes=90, is_active=True)
    admin = User(username="a1", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc1, svc2, admin, client])
    db.flush()
    return svc1, svc2, admin, client


def test_parse_multi_service_lines_with_duration(memory_db) -> None:
    db = memory_db
    svc1, svc2, _, _ = _visit_setup(db)
    lines_json = (
        '[{"service_id": %d, "planned_time": "10:00", "master_ids": [1], "duration_minutes": 240},'
        '{"service_id": %d, "planned_time": "14:00", "master_ids": [1], "duration_minutes": 0}]'
    ) % (svc1.id, svc2.id)
    fp = {
        "planned_time": "10:00",
        "booking_service_lines_json": lines_json,
        "booking_masters_mode": "all",
    }

    class Form:
        def getlist(self, name):
            return ["1"] if name == "booking_master_on" else []

        def get(self, key):
            return fp.get(key)

    err, ids, first_id, specs, on_ids = _parse_booking_visit_lines_and_masters(db, Form(), fp)
    assert err is None
    assert ids == [svc1.id, svc2.id]
    assert first_id == svc1.id
    assert len(specs) == 2
    assert specs[0]["duration_minutes"] == 240
    assert specs[1]["planned_time"] == time(14, 0)
    assert on_ids == [1]


def test_sync_planned_services_persists_duration_and_times(memory_db) -> None:
    db = memory_db
    svc1, svc2, admin, client = _visit_setup(db)
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=datetime(2026, 6, 8, 7, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        planned_service_id=svc1.id,
    )
    db.add(booking)
    db.flush()

    line_specs = [
        {
            "service_id": svc1.id,
            "planned_time": time(10, 0),
            "master_ids": [],
            "comment": "",
            "duration_minutes": 240,
        },
        {
            "service_id": svc2.id,
            "planned_time": time(14, 30),
            "master_ids": [],
            "comment": "вторая",
            "duration_minutes": 0,
        },
    ]
    _sync_booking_planned_services_with_lines(
        db,
        booking_id=booking.id,
        local_day=date(2026, 6, 8),
        tz_name="Europe/Moscow",
        line_specs=line_specs,
    )
    db.commit()

    rows = list(db.scalars(select(BookingPlannedService).where(BookingPlannedService.booking_id == booking.id)).all())
    assert len(rows) == 2
    by_sid = {int(r.service_id): r for r in rows}
    assert by_sid[svc1.id].duration_minutes == 240
    assert by_sid[svc2.id].comment == "вторая"
    assert by_sid[svc2.id].planned_start_time is not None


def test_apply_parsed_visit_lines_updates_fp_duration(memory_db) -> None:
    db = memory_db
    svc1, _, _, _ = _visit_setup(db)
    fp: dict[str, str] = {"planned_time": "10:00"}
    specs = [
        {
            "service_id": svc1.id,
            "planned_time": time(10, 0),
            "master_ids": [1],
            "comment": "",
            "duration_minutes": 240,
        }
    ]
    _apply_parsed_visit_lines_to_fp(fp, specs)
    assert fp["visit_custom_duration_on"] == "1"
    assert fp["visit_custom_duration_h"] == "4"
    assert fp["visit_custom_duration_m"] == "0"
    assert "240" in fp["booking_service_lines_json"]


def test_planned_services_audit_label_includes_duration(memory_db) -> None:
    db = memory_db
    svc1, _, admin, client = _visit_setup(db)
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=datetime(2026, 6, 8, 7, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        planned_service_id=svc1.id,
    )
    db.add(booking)
    db.flush()
    db.add(
        BookingPlannedService(
            booking_id=booking.id,
            service_id=svc1.id,
            sort_order=0,
            planned_start_time=datetime(2026, 6, 8, 7, 0),
            duration_minutes=240,
        )
    )
    db.commit()
    label = _planned_services_audit_label(db, booking.id)
    assert "240 мин" in label
    assert "Услуга 1" in label


def test_line_service_kits_roundtrip_fp_and_details(memory_db) -> None:
    db = memory_db
    svc1, svc2, _, _ = _visit_setup(db)
    sub = db.get(ServiceSubcategory, svc2.subcategory_id)
    assert sub is not None
    sub.show_kit_section = True
    db.commit()

    lines_json = (
        '[{"service_id": %d, "planned_time": "10:00", "master_ids": [1]},'
        '{"service_id": %d, "planned_time": "14:00", "master_ids": [1]}]'
    ) % (svc1.id, svc2.id)
    fp = {
        "kind": BookingKind.VISIT.value,
        "service_id": str(svc1.id),
        "booking_service_lines_json": lines_json,
        "line_1_kit_mode": "IN_STOCK",
        "line_1_stock_kit_id": "42",
        "line_1_stock_kit_pieces": "3",
    }
    collected = _collect_line_service_kits_from_fp(fp)
    assert collected["1"]["kit_mode"] == "IN_STOCK"
    assert collected["1"]["stock_kit_id"] == "42"

    details = _booking_details_from_form(db, fp)
    assert details["line_service_kits"]["1"]["stock_kit_id"] == "42"

    fp2: dict[str, str] = {"line_service_kits": '{"1": {"kit_mode": "ORDER", "order_blanks_qty": "5"}}'}
    _expand_line_service_kits_to_fp(fp2)
    assert fp2["line_1_kit_mode"] == "ORDER"
    assert fp2["line_1_order_blanks_qty"] == "5"


def test_visit_kit_in_stock_validation(memory_db) -> None:
    db = memory_db
    svc1, svc2, _, _ = _visit_setup(db)
    sub = db.get(ServiceSubcategory, svc1.subcategory_id)
    assert sub is not None
    sub.show_kit_section = True
    db.commit()
    svc1 = db.get(Service, svc1.id)
    assert svc1 is not None
    assert service_requires_kit_block(svc1)

    fp_missing = {
        "kind": BookingKind.VISIT.value,
        "service_id": str(svc1.id),
        "visit_kit_mode": "IN_STOCK",
    }
    assert _validate_visit_stock_kit_for_reserve(db, fp_missing) is not None
    assert _visit_kit_mode_is_in_stock(fp_missing)

    fp_ok = dict(fp_missing)
    fp_ok["visit_stock_kit_id"] = "99"
    assert _validate_visit_stock_kit_for_reserve(db, fp_ok) is None

    sub2 = db.get(ServiceSubcategory, svc2.subcategory_id)
    assert sub2 is not None
    sub2.show_kit_section = True
    db.commit()
    lines_json = (
        '[{"service_id": %d, "planned_time": "10:00", "master_ids": [1]},'
        '{"service_id": %d, "planned_time": "14:00", "master_ids": [1]}]'
    ) % (svc1.id, svc2.id)
    fp_line = {
        "kind": BookingKind.VISIT.value,
        "service_id": str(svc1.id),
        "booking_service_lines_json": lines_json,
        "line_1_kit_mode": "IN_STOCK",
    }
    assert _validate_line_service_kits_for_reserve(db, fp_line) is not None
