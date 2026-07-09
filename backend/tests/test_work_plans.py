"""Планы работ: доменная логика, занятость, завершение через работу."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.calendar_occupancy import build_occupancy_for_day
from app.db.models import (
    User,
    UserRole,
    WorkForInventory,
    WorkKind,
    WorkPlan,
    WorkPlanStatus,
    WorkPlanType,
    WorkScope,
)
from app.work_plan import (
    complete_work_plan_from_work,
    master_has_work_plan_conflict,
    work_plan_is_open,
    work_plan_status_label,
    work_plan_type_display,
    work_plan_work_new_query_params,
)


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _add_master(db, username: str = "m1", name: str = "Мастер") -> User:
    u = User(username=username, password_hash="x", display_name=name, role=UserRole.MASTER, is_active=True)
    db.add(u)
    db.flush()
    return u


def test_work_plan_labels() -> None:
    assert work_plan_status_label(WorkPlanStatus.PLANNED) == "Запланировано"
    assert work_plan_status_label(WorkPlanStatus.COMPLETED) == "Выполнено"
    assert work_plan_status_label(WorkPlanStatus.CANCELLED) == "Отменено"


def test_work_plan_type_display(memory_db) -> None:
    db = memory_db
    master = _add_master(db)
    plan = WorkPlan(
        created_by_user_id=master.id,
        planned_date=datetime(2026, 7, 10, 9, 0),
        duration_minutes=60,
        master_id=master.id,
        plan_type=WorkPlanType.WORK_PRODUCT,
        work_kind=WorkKind.KIT,
        status=WorkPlanStatus.PLANNED,
    )
    assert "Работа с товаром" in work_plan_type_display(plan)
    assert "Комплект" in work_plan_type_display(plan)


def test_work_plan_conflict_same_master(memory_db) -> None:
    db = memory_db
    master = _add_master(db)
    start = datetime(2026, 7, 10, 10, 0)
    db.add(
        WorkPlan(
            created_by_user_id=master.id,
            planned_date=start,
            duration_minutes=60,
            master_id=master.id,
            plan_type=WorkPlanType.WORK_PRODUCT,
            work_kind=WorkKind.MIX,
            status=WorkPlanStatus.PLANNED,
        )
    )
    db.commit()
    assert master_has_work_plan_conflict(
        db,
        master_id=master.id,
        start_dt=datetime(2026, 7, 10, 10, 30),
        end_dt=datetime(2026, 7, 10, 11, 30),
    )
    assert not master_has_work_plan_conflict(
        db,
        master_id=master.id,
        start_dt=datetime(2026, 7, 10, 11, 0),
        end_dt=datetime(2026, 7, 10, 12, 0),
    )


def test_occupancy_includes_work_plan(memory_db, monkeypatch) -> None:
    db = memory_db
    master = _add_master(db)
    db.add(
        WorkPlan(
            created_by_user_id=master.id,
            planned_date=datetime(2026, 7, 10, 10, 0),
            duration_minutes=90,
            master_id=master.id,
            plan_type=WorkPlanType.WORK_PRODUCT,
            work_kind=WorkKind.RUBBER,
            status=WorkPlanStatus.PLANNED,
        )
    )
    db.commit()
    monkeypatch.setattr("app.calendar_occupancy.get_display_timezone", lambda _db: "UTC")
    monkeypatch.setattr("app.calendar_occupancy.list_calendar_masters", lambda _db: [{"id": master.id, "name": master.display_name}])
    occ = build_occupancy_for_day(db, day=date(2026, 7, 10), hour_from=9, hour_to=18)
    wp_segs = [s for s in occ["segments"] if s.get("work_plan_id")]
    assert len(wp_segs) == 1
    assert wp_segs[0]["master_id"] == master.id
    assert "Работа с товаром" in wp_segs[0]["service_label"]


def test_complete_work_plan_from_work(memory_db) -> None:
    db = memory_db
    master = _add_master(db)
    plan = WorkPlan(
        created_by_user_id=master.id,
        planned_date=datetime(2026, 7, 10, 14, 0),
        duration_minutes=60,
        master_id=master.id,
        plan_type=WorkPlanType.WORK_PRODUCT,
        work_kind=WorkKind.MIX,
        status=WorkPlanStatus.PLANNED,
    )
    db.add(plan)
    db.flush()
    work = WorkForInventory(
        created_by_user_id=master.id,
        performed_date=datetime(2026, 7, 10, 14, 0),
        kind=WorkKind.MIX,
        scope=WorkScope.IN_STOCK,
    )
    db.add(work)
    db.flush()
    complete_work_plan_from_work(db, plan.id, work.id)
    db.commit()
    db.refresh(plan)
    db.refresh(work)
    assert plan.status == WorkPlanStatus.COMPLETED
    assert work.work_plan_id == plan.id
    assert not work_plan_is_open(plan)


def test_work_plan_work_new_query_params(memory_db, monkeypatch) -> None:
    db = memory_db
    master = _add_master(db)
    plan = WorkPlan(
        created_by_user_id=master.id,
        planned_date=datetime(2026, 7, 10, 8, 30),
        duration_minutes=45,
        master_id=master.id,
        plan_type=WorkPlanType.WORK_PRODUCT,
        work_kind=WorkKind.KIT,
        status=WorkPlanStatus.PLANNED,
    )
    db.add(plan)
    db.commit()
    monkeypatch.setattr("app.work_plan.get_display_timezone", lambda _db: "UTC")
    q = work_plan_work_new_query_params(db, plan)
    assert q["work_plan_id"] == str(plan.id)
    assert q["kind"] == WorkKind.KIT.value
    assert q["performed_date"] == "2026-07-10"


def test_cancelled_plan_not_in_occupancy(memory_db, monkeypatch) -> None:
    db = memory_db
    master = _add_master(db)
    db.add(
        WorkPlan(
            created_by_user_id=master.id,
            planned_date=datetime(2026, 7, 10, 10, 0),
            duration_minutes=60,
            master_id=master.id,
            plan_type=WorkPlanType.WORK_PRODUCT,
            work_kind=WorkKind.MIX,
            status=WorkPlanStatus.CANCELLED,
        )
    )
    db.commit()
    monkeypatch.setattr("app.calendar_occupancy.get_display_timezone", lambda _db: "UTC")
    monkeypatch.setattr("app.calendar_occupancy.list_calendar_masters", lambda _db: [{"id": master.id, "name": master.display_name}])
    occ = build_occupancy_for_day(db, day=date(2026, 7, 10), hour_from=9, hour_to=18)
    assert not [s for s in occ["segments"] if s.get("work_plan_id")]
