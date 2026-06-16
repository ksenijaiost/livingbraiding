from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.calendar_occupancy import build_occupancy_for_day
from app.db.base import Base
from app.db.models import (
    MasterScheduleDay,
    MasterScheduleStatus,
    MasterTimeBlock,
    Setting,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.master_schedule import is_master_available_for_interval
from app.master_time_blocks import (
    TimeBlockValidationError,
    create_time_block,
    delete_time_block,
    list_time_blocks_for_day,
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
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.MASTER))
    return u


def _seed_working_day(db, master: User, d: date) -> None:
    db.add(
        MasterScheduleDay(
            master_id=master.id,
            work_date=d,
            status=MasterScheduleStatus.WORKING,
            time_from=time(9, 0),
            time_to=time(18, 0),
            break_from=time(13, 0),
            break_to=time(14, 0),
        )
    )


def test_create_time_block_and_occupancy(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)
    master = _seed_master(db)
    d = date(2026, 6, 16)
    _seed_working_day(db, master, d)
    db.commit()

    block = create_time_block(
        db,
        master_id=master.id,
        block_date=d,
        time_from=time(10, 0),
        time_to=time(11, 30),
        comment="Работы",
        created_by_user_id=master.id,
    )
    db.commit()

    rows = list_time_blocks_for_day(db, master_id=master.id, block_date=d)
    assert len(rows) == 1
    assert int(rows[0].id) == int(block.id)
    assert rows[0].comment == "Работы"

    occ = build_occupancy_for_day(db, day=d, hour_from=9, hour_to=21, bookings=[])
    blocks = occ.get("block_segments") or []
    assert len(blocks) == 1
    assert blocks[0]["master_id"] == master.id
    assert blocks[0]["comment"] == "Работы"
    assert blocks[0]["start_minutes"] == 10 * 60
    assert blocks[0]["end_minutes"] == 11 * 60 + 30


def test_time_block_blocks_booking_availability(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)
    master = _seed_master(db)
    d = date(2026, 6, 16)
    _seed_working_day(db, master, d)
    create_time_block(
        db,
        master_id=master.id,
        block_date=d,
        time_from=time(10, 0),
        time_to=time(11, 0),
        comment="Подготовка",
        created_by_user_id=master.id,
    )
    db.commit()

    assert (
        is_master_available_for_interval(
            db,
            master_id=master.id,
            start_dt=datetime(2026, 6, 16, 9, 30),
            end_dt=datetime(2026, 6, 16, 10, 15),
        )
        is False
    )
    assert (
        is_master_available_for_interval(
            db,
            master_id=master.id,
            start_dt=datetime(2026, 6, 16, 11, 0),
            end_dt=datetime(2026, 6, 16, 11, 30),
        )
        is True
    )


def test_time_block_validation_errors(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)
    master = _seed_master(db)
    d = date(2026, 6, 16)
    _seed_working_day(db, master, d)
    db.commit()

    with pytest.raises(TimeBlockValidationError):
        create_time_block(
            db,
            master_id=master.id,
            block_date=d,
            time_from=time(12, 30),
            time_to=time(13, 30),
            comment="В перерыве",
            created_by_user_id=master.id,
        )

    create_time_block(
        db,
        master_id=master.id,
        block_date=d,
        time_from=time(10, 0),
        time_to=time(11, 0),
        comment="Первый",
        created_by_user_id=master.id,
    )
    db.commit()

    with pytest.raises(TimeBlockValidationError):
        create_time_block(
            db,
            master_id=master.id,
            block_date=d,
            time_from=time(10, 30),
            time_to=time(11, 30),
            comment="Пересечение",
            created_by_user_id=master.id,
        )


def test_delete_time_block(memory_db) -> None:
    db = memory_db
    _seed_calendar_hours(db)
    master = _seed_master(db)
    d = date(2026, 6, 16)
    _seed_working_day(db, master, d)
    block = create_time_block(
        db,
        master_id=master.id,
        block_date=d,
        time_from=time(15, 0),
        time_to=time(16, 0),
        comment="",
        created_by_user_id=master.id,
    )
    db.commit()

    assert delete_time_block(db, block_id=int(block.id), master_id=master.id) is True
    db.commit()
    assert list_time_blocks_for_day(db, master_id=master.id, block_date=d) == []
    assert db.get(MasterTimeBlock, block.id) is None
