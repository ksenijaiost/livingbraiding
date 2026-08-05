"""Проверка занятости мастера при брони: график + другие брони + планы + блоки."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    BookingStatus,
    Client,
    MasterScheduleDay,
    MasterScheduleStatus,
    MasterTimeBlock,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    WorkKind,
    WorkPlan,
    WorkPlanStatus,
    WorkPlanType,
)
from app.routes.bookings import _validate_booking_visit_line_availability
from app.work_plan import is_master_available_for_booking


@pytest.fixture()
def memory_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    # planned_* в тестах = naive UTC; display TZ = UTC.
    monkeypatch.setattr("app.calendar_occupancy.get_display_timezone", lambda _db: "UTC")

    def _masters(db):
        return [
            {"id": int(u.id), "name": u.display_name or u.username}
            for u in db.scalars(select(User).where(User.role == UserRole.MASTER)).all()
        ]

    monkeypatch.setattr("app.calendar_occupancy.list_calendar_masters", _masters)
    with SessionLocal() as db:
        yield db


def _seed(db):
    admin = User(username="a", display_name="Админ", password_hash="x", role=UserRole.ADMIN_SUPER, is_active=True)
    master = User(username="m", display_name="Таня", password_hash="x", role=UserRole.MASTER, is_active=True)
    db.add_all([admin, master])
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    client = Client(name="Клиент", phone="89001112233", is_confirmed=True, created_by_label="t")
    db.add(client)
    db.flush()
    cat = ServiceCategory(name="Кат")
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(name="Под", category_id=cat.id)
    db.add(sub)
    db.flush()
    svc = Service(name="Услуга", subcategory_id=sub.id, estimated_duration_minutes=120, is_active=True)
    db.add(svc)
    db.flush()
    d = date(2026, 8, 5)
    db.add(
        MasterScheduleDay(
            master_id=master.id,
            work_date=d,
            status=MasterScheduleStatus.WORKING,
            time_from=time(9, 0),
            time_to=time(20, 0),
        )
    )
    db.commit()
    return admin, master, client, svc, d


def _add_visit_booking(db, *, admin_id, client_id, master_id, svc_id, start: datetime):
    b = Booking(
        created_by_user_id=admin_id,
        client_id=client_id,
        planned_date=start,
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
    )
    db.add(b)
    db.flush()
    db.add(BookingMaster(booking_id=b.id, master_id=master_id))
    ps = BookingPlannedService(
        booking_id=b.id,
        service_id=svc_id,
        sort_order=0,
        planned_start_time=start,
        duration_minutes=120,
    )
    db.add(ps)
    db.flush()
    db.add(BookingPlannedServiceMaster(booking_planned_service_id=ps.id, master_id=master_id))
    db.commit()
    return b


def test_booking_blocked_by_existing_visit(memory_db) -> None:
    db = memory_db
    admin, master, client, svc, _d = _seed(db)
    _add_visit_booking(
        db,
        admin_id=admin.id,
        client_id=client.id,
        master_id=master.id,
        svc_id=svc.id,
        start=datetime(2026, 8, 5, 10, 0),
    )
    start = datetime(2026, 8, 5, 10, 0)
    end = start + timedelta(hours=2)
    assert not is_master_available_for_booking(db, master_id=master.id, start_dt=start, end_dt=end)
    assert is_master_available_for_booking(
        db,
        master_id=master.id,
        start_dt=datetime(2026, 8, 5, 12, 0),
        end_dt=datetime(2026, 8, 5, 14, 0),
    )


def test_booking_exclude_self_on_edit(memory_db) -> None:
    db = memory_db
    admin, master, client, svc, _d = _seed(db)
    b = _add_visit_booking(
        db,
        admin_id=admin.id,
        client_id=client.id,
        master_id=master.id,
        svc_id=svc.id,
        start=datetime(2026, 8, 5, 10, 0),
    )
    start = datetime(2026, 8, 5, 10, 0)
    end = start + timedelta(hours=2)
    assert is_master_available_for_booking(
        db,
        master_id=master.id,
        start_dt=start,
        end_dt=end,
        exclude_booking_id=b.id,
    )


def test_booking_blocked_by_work_plan(memory_db) -> None:
    db = memory_db
    admin, master, _client, _svc, _d = _seed(db)
    db.add(
        WorkPlan(
            created_by_user_id=admin.id,
            planned_date=datetime(2026, 8, 5, 11, 0),
            duration_minutes=60,
            master_id=master.id,
            plan_type=WorkPlanType.WORK_PRODUCT,
            work_kind=WorkKind.MIX,
            status=WorkPlanStatus.PLANNED,
        )
    )
    db.commit()
    assert not is_master_available_for_booking(
        db,
        master_id=master.id,
        start_dt=datetime(2026, 8, 5, 10, 30),
        end_dt=datetime(2026, 8, 5, 12, 30),
    )


def test_booking_blocked_by_time_block(memory_db) -> None:
    db = memory_db
    admin, master, _client, _svc, d = _seed(db)
    db.add(
        MasterTimeBlock(
            master_id=master.id,
            block_date=d,
            time_from=time(13, 0),
            time_to=time(14, 0),
            comment="работы",
            created_by_user_id=admin.id,
        )
    )
    db.commit()
    assert not is_master_available_for_booking(
        db,
        master_id=master.id,
        start_dt=datetime(2026, 8, 5, 13, 0),
        end_dt=datetime(2026, 8, 5, 15, 0),
    )


def test_validate_requires_duration(memory_db) -> None:
    db = memory_db
    _admin, master, _client, svc, d = _seed(db)
    svc.estimated_duration_minutes = 0
    db.commit()
    err = _validate_booking_visit_line_availability(
        db,
        local_day=d,
        line_specs=[
            {
                "service_id": svc.id,
                "master_ids": [master.id],
                "planned_time": time(10, 0),
                "duration_minutes": 0,
            }
        ],
        duration_override_minutes=0,
    )
    assert err is not None
    assert "длительн" in err.lower()


def test_validate_rejects_overlap(memory_db) -> None:
    db = memory_db
    admin, master, client, svc, d = _seed(db)
    _add_visit_booking(
        db,
        admin_id=admin.id,
        client_id=client.id,
        master_id=master.id,
        svc_id=svc.id,
        start=datetime(2026, 8, 5, 10, 0),
    )
    err = _validate_booking_visit_line_availability(
        db,
        local_day=d,
        line_specs=[
            {
                "service_id": svc.id,
                "master_ids": [master.id],
                "planned_time": time(10, 0),
                "duration_minutes": 120,
            }
        ],
    )
    assert err is not None
    assert "Таня" in err or "недоступн" in err.lower()
