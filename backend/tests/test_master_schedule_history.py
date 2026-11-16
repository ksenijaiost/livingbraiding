"""Тесты группировки и форматирования истории изменений графика мастера."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import MasterScheduleAuditLog, MasterScheduleStatus, Setting, User, UserRole
from app.master_schedule import apply_weekday_bulk, save_master_schedule_day
from app.master_schedule_history import (
    build_schedule_history_groups,
    detect_change_kind,
    format_day_change_lines,
    format_schedule_status,
    format_schedule_time,
    load_master_schedule_history,
)
from app.setting_keys import CALENDAR_DISPLAY_HOUR_FROM, CALENDAR_DISPLAY_HOUR_TO, DISPLAY_TIMEZONE


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed(db) -> User:
    db.add(Setting(key=CALENDAR_DISPLAY_HOUR_FROM, value="9"))
    db.add(Setting(key=CALENDAR_DISPLAY_HOUR_TO, value="21"))
    db.add(Setting(key=DISPLAY_TIMEZONE, value="Asia/Novosibirsk"))
    u = User(username="m1", display_name="Анна", password_hash="x", role=UserRole.MASTER, is_active=True)
    db.add(u)
    db.flush()
    return u


def _audit(
    *,
    master_id: int,
    work_date: date,
    field_name: str,
    old_value: str | None,
    new_value: str | None,
    changed_at: datetime,
    changed_by_user_id: int | None = None,
    batch_id: str | None = None,
    source: str = "single",
) -> MasterScheduleAuditLog:
    return MasterScheduleAuditLog(
        master_id=master_id,
        work_date=work_date,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        changed_at=changed_at,
        changed_by_user_id=changed_by_user_id,
        batch_id=batch_id,
        source=source,
    )


def test_format_status_and_time_no_none() -> None:
    assert format_schedule_status("WORKING") == "рабочий"
    assert format_schedule_status("DAY_OFF") == "выходной"
    assert format_schedule_status(None) is None
    assert format_schedule_status("None") is None
    assert format_schedule_time("10:00:00") == "10:00"
    assert format_schedule_time("10:00") == "10:00"
    assert format_schedule_time(None) is None


def test_single_save_groups_into_one_row(memory_db) -> None:
    db = memory_db
    master = _seed(db)
    at = datetime(2026, 11, 10, 16, 1, 0)
    d = date(2026, 12, 4)
    db.add_all(
        [
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Статус",
                old_value=None,
                new_value="WORKING",
                changed_at=at,
                changed_by_user_id=master.id,
                batch_id="b1",
            ),
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Начало работы",
                old_value=None,
                new_value="10:00:00",
                changed_at=at,
                changed_by_user_id=master.id,
                batch_id="b1",
            ),
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Конец работы",
                old_value=None,
                new_value="19:00:00",
                changed_at=at,
                changed_by_user_id=master.id,
                batch_id="b1",
            ),
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Перерыв с",
                old_value=None,
                new_value=None,
                changed_at=at,
                changed_by_user_id=master.id,
                batch_id="b1",
            ),
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Перерыв до",
                old_value=None,
                new_value=None,
                changed_at=at,
                changed_by_user_id=master.id,
                batch_id="b1",
            ),
        ]
    )
    db.commit()
    rows = list(db.scalars(select(MasterScheduleAuditLog)).all())
    groups = build_schedule_history_groups(rows, master_id=master.id)
    assert len(groups) == 1
    g = groups[0]
    assert g.change_type == "Создание"
    assert g.work_dates == [d]
    assert g.actor_is_self is True
    assert "Статус: рабочий" in g.change_lines
    assert "Время работы: 10:00–19:00" in g.change_lines
    assert not any("None" in line for line in g.change_lines)
    assert not any("Перерыв" in line for line in g.change_lines)


def test_change_and_delete_kinds() -> None:
    change_rows = [
        _audit(
            master_id=1,
            work_date=date(2026, 1, 1),
            field_name="Статус",
            old_value="WORKING",
            new_value="DAY_OFF",
            changed_at=datetime(2026, 1, 1, 12, 0, 0),
        )
    ]
    assert detect_change_kind(change_rows) == "change"
    assert format_day_change_lines(change_rows, change_kind="change") == ["Статус: рабочий → выходной"]

    delete_rows = [
        _audit(
            master_id=1,
            work_date=date(2026, 1, 1),
            field_name="Начало работы",
            old_value="10:00:00",
            new_value=None,
            changed_at=datetime(2026, 1, 1, 12, 0, 0),
        ),
        _audit(
            master_id=1,
            work_date=date(2026, 1, 1),
            field_name="Конец работы",
            old_value="19:00:00",
            new_value=None,
            changed_at=datetime(2026, 1, 1, 12, 0, 0),
        ),
    ]
    assert detect_change_kind(delete_rows) == "delete"
    lines = format_day_change_lines(delete_rows, change_kind="delete")
    assert lines == ["Время работы: было 10:00–19:00"]


def test_bulk_one_group_with_dates(memory_db) -> None:
    db = memory_db
    master = _seed(db)
    date_from = date(2026, 5, 23)  # Fri
    date_to = date(2026, 5, 26)  # Tue
    apply_weekday_bulk(
        db,
        master_id=master.id,
        date_from=date_from,
        date_to=date_to,
        working_weekdays={0},  # Mon
        time_from=time(10, 0),
        time_to=time(19, 0),
        break_from=None,
        break_to=None,
        changed_by_user_id=master.id,
    )
    db.commit()

    rows = list(db.scalars(select(MasterScheduleAuditLog)).all())
    assert rows
    batch_ids = {r.batch_id for r in rows}
    assert len(batch_ids) == 1
    assert None not in batch_ids
    sources = {r.source for r in rows}
    assert sources == {"bulk_weekday"}
    changed_ats = {r.changed_at for r in rows}
    assert len(changed_ats) == 1

    groups = build_schedule_history_groups(rows, master_id=master.id)
    assert len(groups) == 1
    g = groups[0]
    assert g.change_type == "Массовое создание"
    assert sorted(g.work_dates) == [
        date(2026, 5, 23),
        date(2026, 5, 24),
        date(2026, 5, 25),
        date(2026, 5, 26),
    ]
    assert g.actor_is_self is True
    assert g.change_lines


def test_self_master_badge_and_studio_tz(memory_db) -> None:
    db = memory_db
    master = _seed(db)
    admin = User(
        username="a1",
        display_name="Админ",
        password_hash="x",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db.add(admin)
    db.flush()

    # 16:01 UTC → 23:01 Asia/Novosibirsk
    at = datetime(2026, 11, 10, 16, 1, 0)
    save_master_schedule_day(
        db,
        master_id=master.id,
        work_date=date(2026, 12, 4),
        status=MasterScheduleStatus.WORKING,
        time_from=time(10, 0),
        time_to=time(19, 0),
        break_from=None,
        break_to=None,
        changed_by_user_id=master.id,
        audit_changed_at=at,
    )
    db.commit()

    payload = load_master_schedule_history(
        db,
        master_id=master.id,
        changed_from=datetime(2026, 1, 1),
        offset=0,
        limit=20,
    )
    assert payload["master_name"] == "Анна"
    assert payload["total_groups"] == 1
    g0 = payload["groups"][0]
    assert g0["change_type"] == "Создание"
    assert g0["changed_at"] == "10.11.2026 23:01"
    assert g0["work_dates"] == ["04.12.2026, пт"]
    assert g0["actor_is_self"] is True
    assert g0["actor_name"] == "Анна"
    assert "рабочий" in "\n".join(g0["change_lines"])
    assert "None" not in "\n".join(g0["change_lines"])

    # Изменение админом — без бейджа «сам мастер»
    save_master_schedule_day(
        db,
        master_id=master.id,
        work_date=date(2026, 12, 4),
        status=MasterScheduleStatus.DAY_OFF,
        time_from=None,
        time_to=None,
        break_from=None,
        break_to=None,
        changed_by_user_id=admin.id,
        audit_changed_at=at + timedelta(minutes=5),
    )
    db.commit()
    payload2 = load_master_schedule_history(
        db,
        master_id=master.id,
        changed_from=datetime(2026, 1, 1),
        offset=0,
        limit=20,
    )
    assert payload2["total_groups"] == 2
    newest = payload2["groups"][0]
    assert newest["change_type"] == "Изменение"
    assert newest["actor_is_self"] is False
    assert newest["actor_name"] == "Админ"


def test_old_rows_without_batch_group_by_changed_at(memory_db) -> None:
    db = memory_db
    master = _seed(db)
    at = datetime(2026, 6, 1, 10, 0, 0)
    d = date(2026, 6, 2)
    db.add_all(
        [
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Статус",
                old_value="WORKING",
                new_value="DAY_OFF",
                changed_at=at,
                changed_by_user_id=None,
                batch_id=None,
                source="single",
            ),
            _audit(
                master_id=master.id,
                work_date=d,
                field_name="Начало работы",
                old_value="10:00:00",
                new_value=None,
                changed_at=at,
                changed_by_user_id=None,
                batch_id=None,
                source="single",
            ),
        ]
    )
    db.commit()
    groups = build_schedule_history_groups(
        list(db.scalars(select(MasterScheduleAuditLog)).all()),
        master_id=master.id,
    )
    assert len(groups) == 1
    assert groups[0].change_type == "Изменение"
    assert groups[0].actor_name is None
    ddict = groups[0].to_dict(tz_name="Asia/Novosibirsk")
    assert ddict["actor_name"] == "система"
