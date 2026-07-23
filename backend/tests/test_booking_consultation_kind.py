"""Бронь типа CONSULTATION (Fix 97)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.consultation_booking import can_create_consultation_from_booking, consultation_for_booking
from app.consultation_types import types_json_dumps
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStatus,
    Client,
    Consultation,
    MasterScheduleDay,
    MasterScheduleStatus,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.planned_services_db import booking_planned_service_ids
from app.routes.bookings import (
    CONSULTATION_BOOKING_DEFAULT_DURATION_MINUTES,
    _booking_list_master_labels,
    _consultation_booking_duration_minutes,
    _parse_consultation_master_id,
    _validate_consultation_booking_availability,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed(db):
    admin = User(
        username="a",
        password_hash="x",
        display_name="Admin",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    master = User(
        username="m",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(admin)
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    client = Client(name="C", phone="+79990000002", is_confirmed=True)
    db.add(client)
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.add(
        MasterScheduleDay(
            master_id=master.id,
            work_date=date(2026, 4, 15),
            status=MasterScheduleStatus.WORKING,
            time_from=time(9, 0),
            time_to=time(18, 0),
        )
    )
    db.commit()
    return int(admin.id), int(client.id), int(master.id)


def test_consultation_booking_and_link(memory_db) -> None:
    db = memory_db
    _admin_id, client_id, _master_id = _seed(db)
    planned = datetime(2026, 4, 15, 10, 30, 0)
    b = Booking(
        created_by_user_id=_admin_id,
        client_id=client_id,
        planned_date=planned,
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.ACTIVE,
        details_json=types_json_dumps({"BRAIDING": True}),
    )
    db.add(b)
    db.commit()
    db.refresh(b)

    assert can_create_consultation_from_booking(db, b.id)
    c = Consultation(
        created_by_user_id=_admin_id,
        client_id=client_id,
        source_booking_id=b.id,
        consultation_date=planned,
        types_json=types_json_dumps({"BRAIDING": True}),
    )
    db.add(c)
    db.commit()

    linked = consultation_for_booking(db, b.id)
    assert linked is not None
    assert int(linked.id) == int(c.id)
    assert not can_create_consultation_from_booking(db, b.id)
    assert booking_planned_service_ids(db, b.id) == []


def test_consultation_booking_master_required(memory_db) -> None:
    db = memory_db
    _admin_id, _client_id, master_id = _seed(db)
    err, mid = _parse_consultation_master_id(db, {})
    assert err is not None
    assert mid is None

    err2, mid2 = _parse_consultation_master_id(db, {"consultation_master_id": str(master_id)})
    assert err2 is None
    assert mid2 == master_id


def test_consultation_booking_default_duration(memory_db) -> None:
    assert _consultation_booking_duration_minutes({}) == CONSULTATION_BOOKING_DEFAULT_DURATION_MINUTES
    assert _consultation_booking_duration_minutes({"consultation_duration_on": "1", "consultation_duration_h": "2", "consultation_duration_m": "30"}) == 150


def test_consultation_booking_availability(memory_db) -> None:
    db = memory_db
    _admin_id, _client_id, master_id = _seed(db)
    ok = _validate_consultation_booking_availability(
        db,
        local_day=date(2026, 4, 15),
        planned_time="10:30",
        master_id=master_id,
        duration_minutes=60,
    )
    assert ok is None

    bad = _validate_consultation_booking_availability(
        db,
        local_day=date(2026, 4, 15),
        planned_time="17:30",
        master_id=master_id,
        duration_minutes=60,
    )
    assert bad is not None


def test_consultation_booking_stores_master(memory_db) -> None:
    db = memory_db
    admin_id, client_id, master_id = _seed(db)
    planned = datetime(2026, 4, 15, 10, 30, 0)
    b = Booking(
        created_by_user_id=admin_id,
        client_id=client_id,
        planned_date=planned,
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.ACTIVE,
        details_json='{"consultation_duration_minutes": "60"}',
    )
    db.add(b)
    db.flush()
    db.add(BookingMaster(booking_id=b.id, master_id=master_id))
    db.commit()
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    loaded = db.scalar(select(Booking).where(Booking.id == b.id).options(selectinload(Booking.masters)))
    assert loaded is not None
    assert [int(x.master_id) for x in loaded.masters] == [master_id]


def test_booking_list_master_labels(memory_db) -> None:
    db = memory_db
    admin_id, client_id, master_id = _seed(db)
    master2 = User(
        username="m2",
        password_hash="x",
        display_name="Анна",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(master2)
    db.flush()
    db.add(UserRoleAssignment(user_id=master2.id, role=UserRole.MASTER))
    planned = datetime(2026, 4, 15, 10, 30, 0)
    b = Booking(
        created_by_user_id=admin_id,
        client_id=client_id,
        planned_date=planned,
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.ACTIVE,
        details_json='{"visit_order_master_ids": "' + str(master2.id) + '"}',
    )
    db.add(b)
    db.flush()
    db.add(BookingMaster(booking_id=b.id, master_id=master_id))
    db.commit()
    labels = _booking_list_master_labels(db, [b])
    assert "Master" in labels[int(b.id)]
    assert "Анна" in labels[int(b.id)]


def test_auto_complete_consultation_booking_after_consultation(memory_db) -> None:
    from app.routes.bookings import booking_linked_need_consultation, try_auto_complete_booking

    db = memory_db
    admin_id, client_id, master_id = _seed(db)
    planned = datetime(2026, 4, 15, 10, 0, 0)
    b = Booking(
        created_by_user_id=admin_id,
        client_id=client_id,
        planned_date=planned,
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.ACTIVE,
    )
    db.add(b)
    db.commit()
    db.refresh(b)

    assert booking_linked_need_consultation(b) is True
    try_auto_complete_booking(db, int(b.id))
    db.commit()
    db.refresh(b)
    assert b.status == BookingStatus.ACTIVE

    cons = Consultation(
        created_at=datetime(2026, 4, 15),
        created_by_user_id=master_id,
        client_id=client_id,
        source_booking_id=b.id,
        consultation_date=planned,
        types_json=types_json_dumps({"BRAIDING": True}),
    )
    db.add(cons)
    db.commit()

    try_auto_complete_booking(db, int(b.id))
    db.commit()
    db.refresh(b)
    assert b.status == BookingStatus.DONE
