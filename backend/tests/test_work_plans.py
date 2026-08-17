"""Планы работ: доменная логика, занятость, завершение через работу."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.calendar_occupancy import build_occupancy_for_day
from app.db.models import (
    HourlyWorkEntry,
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
    complete_work_plan_from_hourly_work,
    complete_work_plan_from_work,
    linked_hourly_work_for_plan,
    linked_work_for_plan,
    master_has_work_plan_conflict,
    missing_planned_masters_confirm_text,
    require_work_plan_missing_masters_ack,
    work_plan_hourly_new_query_params,
    work_plan_is_open,
    work_plan_status_emoji,
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
    assert work_plan_status_emoji(WorkPlanStatus.PLANNED) == "⌛"
    assert work_plan_status_emoji(WorkPlanStatus.COMPLETED) == "✅"
    assert work_plan_status_emoji(WorkPlanStatus.CANCELLED) == "❌"


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


def test_work_plan_type_display_hourly(memory_db) -> None:
    db = memory_db
    master = _add_master(db)
    plan = WorkPlan(
        created_by_user_id=master.id,
        planned_date=datetime(2026, 7, 10, 9, 0),
        duration_minutes=60,
        master_id=master.id,
        plan_type=WorkPlanType.HOURLY,
        work_kind=None,
        status=WorkPlanStatus.PLANNED,
    )
    assert work_plan_type_display(plan) == "Почасовая работа"


def test_work_plan_hourly_new_query_params(memory_db, monkeypatch) -> None:
    db = memory_db
    master = _add_master(db)
    plan = WorkPlan(
        created_by_user_id=master.id,
        planned_date=datetime(2026, 7, 10, 8, 30),
        duration_minutes=75,
        master_id=master.id,
        plan_type=WorkPlanType.HOURLY,
        work_kind=None,
        comment="уборка",
        status=WorkPlanStatus.PLANNED,
    )
    db.add(plan)
    db.commit()
    monkeypatch.setattr("app.work_plan.get_display_timezone", lambda _db: "UTC")
    q = work_plan_hourly_new_query_params(db, plan)
    assert q["work_plan_id"] == str(plan.id)
    assert q["performed_date"] == "2026-07-10"
    assert q["duration_h"] == "1"
    assert q["duration_m"] == "15"
    assert q["master_id"] == str(master.id)
    assert "план работ" in q["comment"]


def test_complete_work_plan_from_hourly_work(memory_db) -> None:
    db = memory_db
    master = _add_master(db)
    plan = WorkPlan(
        created_by_user_id=master.id,
        planned_date=datetime(2026, 7, 10, 14, 0),
        duration_minutes=60,
        master_id=master.id,
        plan_type=WorkPlanType.HOURLY,
        work_kind=None,
        status=WorkPlanStatus.PLANNED,
    )
    db.add(plan)
    db.flush()
    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 10, 14, 0),
        duration_minutes=60,
        amount=500.0,
        master_user_id=master.id,
    )
    db.add(entry)
    db.flush()
    complete_work_plan_from_hourly_work(db, plan.id, entry.id)
    db.commit()
    db.refresh(plan)
    db.refresh(entry)
    assert plan.status == WorkPlanStatus.COMPLETED
    assert entry.work_plan_id == plan.id
    assert linked_hourly_work_for_plan(db, plan.id) is not None
    assert not work_plan_is_open(plan)


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
    from app.calendar_occupancy import COLOR_OCCUPANCY_WORK_PLAN

    occ = build_occupancy_for_day(db, day=date(2026, 7, 10), hour_from=9, hour_to=18)
    wp_segs = [s for s in occ["segments"] if s.get("work_plan_id")]
    assert len(wp_segs) == 1
    assert wp_segs[0]["master_id"] == master.id
    assert wp_segs[0]["color"] == COLOR_OCCUPANCY_WORK_PLAN
    assert occ["colors"]["work_plan"] == COLOR_OCCUPANCY_WORK_PLAN
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


def _sibling_kit_plans(db, creator: User, masters: list[User], *, created_at: datetime, planned_dates: list[datetime] | None = None) -> list[WorkPlan]:
    plans: list[WorkPlan] = []
    for i, master in enumerate(masters):
        planned = (planned_dates[i] if planned_dates else datetime(2026, 7, 10, 14, 0))
        plan = WorkPlan(
            created_by_user_id=creator.id,
            created_at=created_at,
            planned_date=planned,
            duration_minutes=60,
            master_id=master.id,
            plan_type=WorkPlanType.WORK_PRODUCT,
            work_kind=WorkKind.KIT,
            status=WorkPlanStatus.PLANNED,
        )
        db.add(plan)
        plans.append(plan)
    db.flush()
    return plans


def test_complete_work_plan_closes_all_sibling_masters(memory_db) -> None:
    db = memory_db
    author = _add_master(db, "author", "Автор")
    m1 = _add_master(db, "m1", "Анна")
    m2 = _add_master(db, "m2", "Борис")
    batch_at = datetime(2026, 7, 9, 12, 0, 0)
    p1, p2 = _sibling_kit_plans(db, author, [m1, m2], created_at=batch_at)
    work = WorkForInventory(
        created_by_user_id=author.id,
        performed_date=datetime(2026, 7, 10, 14, 0),
        kind=WorkKind.KIT,
        scope=WorkScope.IN_STOCK,
    )
    db.add(work)
    db.flush()
    complete_work_plan_from_work(db, p1.id, work.id)
    db.commit()
    db.refresh(p1)
    db.refresh(p2)
    db.refresh(work)
    assert p1.status == WorkPlanStatus.COMPLETED
    assert p2.status == WorkPlanStatus.COMPLETED
    assert work.work_plan_id == p1.id
    assert linked_work_for_plan(db, p2.id) is work


def test_complete_work_plan_closes_siblings_with_different_planned_date(memory_db) -> None:
    db = memory_db
    author = _add_master(db, "author", "Автор")
    m1 = _add_master(db, "m1", "Анна")
    m2 = _add_master(db, "m2", "Борис")
    batch_at = datetime(2026, 7, 9, 12, 0, 0)
    p1, p2 = _sibling_kit_plans(
        db,
        author,
        [m1, m2],
        created_at=batch_at,
        planned_dates=[datetime(2026, 7, 10, 14, 0), datetime(2026, 7, 11, 9, 0)],
    )
    work = WorkForInventory(
        created_by_user_id=author.id,
        performed_date=datetime(2026, 7, 10, 14, 0),
        kind=WorkKind.KIT,
        scope=WorkScope.IN_STOCK,
    )
    db.add(work)
    db.flush()
    complete_work_plan_from_work(db, p1.id, work.id)
    db.commit()
    db.refresh(p1)
    db.refresh(p2)
    assert p1.status == WorkPlanStatus.COMPLETED
    assert p2.status == WorkPlanStatus.COMPLETED


def test_complete_work_plan_does_not_close_unrelated_same_day_plan(memory_db) -> None:
    db = memory_db
    author = _add_master(db, "author", "Автор")
    m1 = _add_master(db, "m1", "Анна")
    m2 = _add_master(db, "m2", "Борис")
    batch_at = datetime(2026, 7, 9, 12, 0, 0)
    p1, p2 = _sibling_kit_plans(db, author, [m1, m2], created_at=batch_at)
    other = WorkPlan(
        created_by_user_id=author.id,
        created_at=datetime(2026, 7, 9, 15, 0, 0),
        planned_date=datetime(2026, 7, 10, 16, 0),
        duration_minutes=60,
        master_id=m2.id,
        plan_type=WorkPlanType.WORK_PRODUCT,
        work_kind=WorkKind.KIT,
        status=WorkPlanStatus.PLANNED,
    )
    db.add(other)
    work = WorkForInventory(
        created_by_user_id=author.id,
        performed_date=datetime(2026, 7, 10, 14, 0),
        kind=WorkKind.KIT,
        scope=WorkScope.IN_STOCK,
    )
    db.add(work)
    db.flush()
    complete_work_plan_from_work(db, p1.id, work.id)
    db.commit()
    db.refresh(p1)
    db.refresh(p2)
    db.refresh(other)
    assert p1.status == WorkPlanStatus.COMPLETED
    assert p2.status == WorkPlanStatus.COMPLETED
    assert other.status == WorkPlanStatus.PLANNED


def test_missing_planned_masters_confirm_text() -> None:
    assert missing_planned_masters_confirm_text([]) == ""
    assert missing_planned_masters_confirm_text(["Анна"]) == (
        "Вы сохраняете работу, но на неё был запланирован мастер Анна. "
        "Точно сохранить без него?"
    )
    assert missing_planned_masters_confirm_text(["Анна", "Борис"]) == (
        "Вы сохраняете работу, но на неё были запланированы мастера Анна, Борис. "
        "Точно сохранить без них?"
    )


def test_require_work_plan_missing_masters_ack(memory_db) -> None:
    db = memory_db
    author = _add_master(db, "author", "Автор")
    m1 = _add_master(db, "m1", "Анна")
    m2 = _add_master(db, "m2", "Борис")
    p1, _p2 = _sibling_kit_plans(db, author, [m1, m2], created_at=datetime(2026, 7, 9, 12, 0, 0))
    db.commit()
    with pytest.raises(ValueError, match="Анна"):
        require_work_plan_missing_masters_ack(db, p1.id, {m2.id}, None)
    require_work_plan_missing_masters_ack(db, p1.id, {m1.id, m2.id}, None)
    require_work_plan_missing_masters_ack(db, p1.id, {m2.id}, "1")


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


def test_parse_work_product_master_ids_multi() -> None:
    from starlette.datastructures import FormData

    from app.routes.work_plans import _parse_work_product_master_ids

    form = FormData([("master_ids", "3"), ("master_ids", "5"), ("master_ids", "3")])
    assert _parse_work_product_master_ids(form, is_admin=True, current_user_id=1) == [3, 5]


def test_parse_work_product_master_ids_self_default() -> None:
    from starlette.datastructures import FormData

    from app.routes.work_plans import _parse_work_product_master_ids

    form = FormData([])
    assert _parse_work_product_master_ids(form, is_admin=False, current_user_id=42) == [42]


def test_parse_work_product_master_ids_requires_selection() -> None:
    from starlette.datastructures import FormData

    from app.routes.work_plans import _parse_work_product_master_ids

    with pytest.raises(ValueError, match="хотя бы одного"):
        _parse_work_product_master_ids(FormData([]), is_admin=True, current_user_id=1)


def test_parse_hourly_master_id_single() -> None:
    from starlette.datastructures import FormData

    from app.routes.work_plans import _parse_hourly_master_id

    assert _parse_hourly_master_id(FormData([]), is_admin=False, current_user_id=7) == 7
    assert (
        _parse_hourly_master_id(
            FormData([("pick_other_master", "1"), ("master_id", "9")]),
            is_admin=False,
            current_user_id=7,
        )
        == 9
    )
