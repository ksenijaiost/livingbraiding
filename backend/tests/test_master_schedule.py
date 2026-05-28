from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.calendar_occupancy import build_occupancy_for_day
from app.master_schedule import (
    apply_weekday_bulk,
    day_state,
    is_master_available_for_interval,
    master_unavailable_for_day,
    schedule_filled_until,
)
from app.db.base import Base
from app.db.models import (
    MasterScheduleDay,
    MasterScheduleStatus,
    Setting,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.setting_keys import CALENDAR_DISPLAY_HOUR_FROM, CALENDAR_DISPLAY_HOUR_TO


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_calendar_hours(db) -> None:
    db.add(Setting(key=CALENDAR_DISPLAY_HOUR_FROM, value="9"))
    db.add(Setting(key=CALENDAR_DISPLAY_HOUR_TO, value="21"))
    db.commit()


def _seed_master(db) -> User:
    u = User(username="m1", display_name="Мастер", password_hash="x", role=UserRole.MASTER, is_active=True)
    db.add(u)
    db.flush()
    return u


def test_master_unavailable_intervals_and_availability(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)

    master = _seed_master(db)

    d = date(2026, 5, 23)
    db.add(
        MasterScheduleDay(
            master_id=master.id,
            work_date=d,
            status=MasterScheduleStatus.WORKING,
            time_from=time(10, 0),
            time_to=time(12, 0),
            break_from=time(11, 0),
            break_to=time(11, 30),
        )
    )
    db.commit()

    col_state, unavailable = master_unavailable_for_day(db, master_id=master.id, d=d, hour_from=9, hour_to=21)
    assert col_state == "working"
    # Ожидаем: [09-10], [12-21], [11-11:30]
    segs = sorted([(u.start_minutes, u.end_minutes) for u in unavailable], key=lambda x: x[0])
    assert segs == [(540, 600), (660, 690), (720, 1260)]

    # Интервал 10:30–11:00 (заканчивается в начале перерыва) — доступен.
    assert (
        is_master_available_for_interval(
            db,
            master_id=master.id,
            start_dt=datetime(2026, 5, 23, 10, 30),
            end_dt=datetime(2026, 5, 23, 11, 0),
        )
        is True
    )

    # 10:30–11:15 — пересечение с перерывом запрещено.
    assert (
        is_master_available_for_interval(
            db,
            master_id=master.id,
            start_dt=datetime(2026, 5, 23, 10, 30),
            end_dt=datetime(2026, 5, 23, 11, 15),
        )
        is False
    )

    # 11:30–11:45 (после перерыва) — доступен.
    assert (
        is_master_available_for_interval(
            db,
            master_id=master.id,
            start_dt=datetime(2026, 5, 23, 11, 30),
            end_dt=datetime(2026, 5, 23, 11, 45),
        )
        is True
    )

    # 11:15–11:45 — пересечение с перерывом.
    assert (
        is_master_available_for_interval(
            db,
            master_id=master.id,
            start_dt=datetime(2026, 5, 23, 11, 15),
            end_dt=datetime(2026, 5, 23, 11, 45),
        )
        is False
    )


def test_apply_weekday_bulk_creates_day_off_and_filled_until(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)
    master = _seed_master(db)

    date_from = date(2026, 5, 23)  # Fri
    date_to = date(2026, 5, 26)  # Tue
    updated = apply_weekday_bulk(
        db,
        master_id=master.id,
        date_from=date_from,
        date_to=date_to,
        working_weekdays={0},  # только Пн
        time_from=None,
        time_to=None,
        break_from=None,
        break_to=None,
        changed_by_user_id=master.id,
    )
    assert updated == 4
    db.commit()

    assert day_state(db, master_id=master.id, d=date_from) == "day_off"
    # Пн в диапазоне — working, остальные в диапазоне — day_off.
    monday = date(2026, 5, 25)
    assert day_state(db, master_id=master.id, d=monday) == "working"
    assert day_state(db, master_id=master.id, d=date_to) == "day_off"
    assert schedule_filled_until(db, master_id=master.id) == date_to

    cnt = db.scalar(
        select(func.count(MasterScheduleDay.id)).where(
            MasterScheduleDay.master_id == master.id,
        )
    )
    assert cnt == 4


def test_calendar_occupancy_returns_schedule_overlay(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)
    master = _seed_master(db)
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    db.commit()

    d = date(2026, 5, 23)
    db.add(
        MasterScheduleDay(
            master_id=master.id,
            work_date=d,
            status=MasterScheduleStatus.WORKING,
            time_from=time(10, 0),
            time_to=time(12, 0),
            break_from=None,
            break_to=None,
        )
    )
    db.commit()

    occ = build_occupancy_for_day(db, day=d, hour_from=9, hour_to=21, bookings=[])
    assert "schedule" in occ
    assert str(master.id) in occ["schedule"]
    sc = occ["schedule"][str(master.id)]
    assert sc["column_state"] == "working"
    # Должны быть затемнены вне смены: [09-10] и [12-21]
    un = sorted([(x["start_minutes"], x["end_minutes"]) for x in sc["unavailable"]], key=lambda x: x[0])
    assert un == [(540, 600), (720, 1260)]

