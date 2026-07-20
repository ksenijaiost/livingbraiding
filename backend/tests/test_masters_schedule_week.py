from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStatus,
    Client,
    MasterScheduleDay,
    MasterScheduleStatus,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.masters_schedule_week import build_masters_schedule_week, monday_of_week, master_initials


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_monday_of_week() -> None:
    assert monday_of_week(date(2026, 7, 7)) == date(2026, 7, 6)
    assert monday_of_week(date(2026, 7, 6)) == date(2026, 7, 6)


def test_master_initials() -> None:
    u = User(username="a", display_name="Малыгин Дмитрий")
    assert master_initials(u) == "МД"


def test_build_week_grid_states(db) -> None:
    m = User(username="m1", display_name="Master One", role=UserRole.MASTER, password_hash="x", is_active=True)
    db.add(m)
    db.flush()
    db.add(UserRoleAssignment(user_id=m.id, role=UserRole.MASTER))
    c = Client(name="Клиент")
    db.add(c)
    db.flush()
    mon = date(2026, 7, 6)
    db.add(
        MasterScheduleDay(
            master_id=m.id,
            work_date=mon,
            status=MasterScheduleStatus.WORKING,
            time_from=None,
            time_to=None,
        )
    )
    db.add(
        Booking(
            client_id=c.id,
            created_by_user_id=m.id,
            planned_date=__import__("datetime").datetime(2026, 7, 6, 10, 0, 0),
            kind=BookingKind.VISIT,
            status=BookingStatus.ACTIVE,
        )
    )
    db.flush()
    b = db.query(Booking).one()
    db.add(BookingMaster(booking_id=b.id, master_id=m.id))
    db.commit()

    data = build_masters_schedule_week(db, week_start=mon)
    assert data["week_start"] == "2026-07-06"
    assert len(data["masters"]) == 1
    cells = data["masters"][0]["cells"]
    assert cells[0]["state"] == "working"
    assert cells[0]["has_booking"] is True
    assert cells[0]["has_free_time"] is True
    assert cells[1]["state"] in ("day_off", "no_data")


def test_working_day_without_booking_has_free_time(db) -> None:
    from datetime import time as dt_time

    m = User(username="m2", display_name="Master Two", role=UserRole.MASTER, password_hash="x", is_active=True)
    db.add(m)
    db.flush()
    db.add(UserRoleAssignment(user_id=m.id, role=UserRole.MASTER))
    mon = date(2026, 7, 13)
    db.add(
        MasterScheduleDay(
            master_id=m.id,
            work_date=mon,
            status=MasterScheduleStatus.WORKING,
            time_from=dt_time(10, 0),
            time_to=dt_time(18, 0),
        )
    )
    db.commit()

    data = build_masters_schedule_week(db, week_start=mon)
    cells = data["masters"][0]["cells"]
    assert cells[0]["state"] == "working"
    assert cells[0]["has_booking"] is False
    assert cells[0]["has_free_time"] is True
