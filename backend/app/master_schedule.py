"""
График работы мастеров (рабочий/выходной + интервал и перерыв).

Это доменный слой: валидация доступности, вычисление затемнений для «Занятости»,
а также развёртка массовых настроек.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit_field_labels import audit_field_label
from app.calendar_display import get_calendar_display_hours
from app.db.models import MasterScheduleAuditLog, MasterScheduleDay, MasterScheduleStatus


ScheduleColumnState = Literal["no_data", "day_off", "working"]


@dataclass(frozen=True)
class TimeRangeMinutes:
    start_minutes: int
    end_minutes: int


def get_default_work_hours(db: Session) -> tuple[int, int]:
    """Часы сетки занятости: [from, to)."""
    return get_calendar_display_hours(db)


def _time_to_minutes(t: time | None) -> int | None:
    if t is None:
        return None
    return int(t.hour) * 60 + int(t.minute)


def resolve_day_interval(db: Session, row: MasterScheduleDay | None) -> tuple[int, int]:
    """
    Преобразует запись графика в интервал минут внутри дня.

    Если time_from/time_to не заданы — используются часы по умолчанию из настроек календаря.
    """
    hour_from, hour_to = get_default_work_hours(db)
    default_start = hour_from * 60
    default_end = hour_to * 60

    if row is None:
        return default_start, default_end

    start_min = _time_to_minutes(row.time_from) or default_start
    end_min = _time_to_minutes(row.time_to) or default_end

    # Нормализация "сломанных" вводов.
    if end_min <= start_min:
        start_min, end_min = default_start, default_end

    return start_min, end_min


def day_state(db: Session, *, master_id: int, d: date) -> ScheduleColumnState:
    row = db.scalar(
        select(MasterScheduleDay).where(
            MasterScheduleDay.master_id == master_id, MasterScheduleDay.work_date == d
        )
    )
    if row is None:
        # Правило из ТЗ: "все дни между заполненными днями = выходной".
        # Реализуем как: если дата попадает в [min(work_date), max(work_date)] для мастера,
        # но строки нет — считаем DAY_OFF. Даты после max остаются NO_DATA.
        bounds = db.execute(
            select(
                func.min(MasterScheduleDay.work_date),
                func.max(MasterScheduleDay.work_date),
            ).where(MasterScheduleDay.master_id == master_id)
        ).one()
        min_d, max_d = bounds[0], bounds[1]
        if min_d and max_d and min_d <= d <= max_d:
            return "day_off"
        return "no_data"
    if row.status == MasterScheduleStatus.DAY_OFF:
        return "day_off"
    return "working"


def schedule_filled_until(db: Session, *, master_id: int) -> date | None:
    """Последняя дата, для которой мастер уже имеет записи в графике (рабочий или выходной)."""
    v = db.scalar(
        select(MasterScheduleDay.work_date)
        .where(MasterScheduleDay.master_id == master_id)
        .order_by(MasterScheduleDay.work_date.desc())
        .limit(1)
    )
    return v


def master_unavailable_for_day(
    db: Session,
    *,
    master_id: int,
    d: date,
    hour_from: int,
    hour_to: int,
) -> tuple[ScheduleColumnState, list[TimeRangeMinutes]]:
    """
    Возвращает состояние колонки и список "нерабочих" интервалов для затемнения:
    - для no_data: (no_data, [])
    - для day_off: (day_off, [])
    - для working: интервалы вне рабочей смены + перерыв (перерыв тоже затемняем).
    """
    st = day_state(db, master_id=master_id, d=d)
    if st != "working":
        return st, []

    row = db.scalar(
        select(MasterScheduleDay).where(
            MasterScheduleDay.master_id == master_id, MasterScheduleDay.work_date == d
        )
    )
    assert row is not None

    work_start, work_end = resolve_day_interval(db, row)
    grid_start = hour_from * 60
    grid_end = hour_to * 60

    unavailable: list[TimeRangeMinutes] = []

    # Вне интервала смены
    if work_start > grid_start:
        unavailable.append(TimeRangeMinutes(start_minutes=grid_start, end_minutes=min(work_start, grid_end)))
    if work_end < grid_end:
        unavailable.append(TimeRangeMinutes(start_minutes=max(work_end, grid_start), end_minutes=grid_end))

    # Перерыв
    break_start = _time_to_minutes(row.break_from)
    break_end = _time_to_minutes(row.break_to)
    if break_start is not None and break_end is not None and break_end > break_start:
        # затемняем пересечение перерыва с сеткой занятости
        bs = max(break_start, grid_start)
        be = min(break_end, grid_end)
        if be > bs:
            unavailable.append(TimeRangeMinutes(start_minutes=bs, end_minutes=be))

    return "working", unavailable


def _interval_overlaps(range_a: TimeRangeMinutes, range_b: TimeRangeMinutes) -> bool:
    return not (range_a.end_minutes <= range_b.start_minutes or range_b.end_minutes <= range_a.start_minutes)


def is_master_available_for_interval(
    db: Session,
    *,
    master_id: int,
    start_dt: datetime,
    end_dt: datetime,
) -> bool:
    """
    Доступность мастера для брони:
    - мастер должен иметь запись для каждого затронутого дня
    - status=WORKING
    - весь интервал услуги должен лежать внутри рабочей смены
    - при перерыве интервал не должен пересекать перерыв (частичное попадание запрещено)
    """
    if end_dt <= start_dt:
        return False

    # В этом MVP считаем, что услуга не переходит через полночь.
    if start_dt.date() != end_dt.date():
        return False

    d = start_dt.date()
    st = day_state(db, master_id=master_id, d=d)
    if st != "working":
        return False

    row = db.scalar(
        select(MasterScheduleDay).where(
            MasterScheduleDay.master_id == master_id, MasterScheduleDay.work_date == d
        )
    )
    assert row is not None

    work_start, work_end = resolve_day_interval(db, row)
    start_m = start_dt.hour * 60 + start_dt.minute
    end_m = end_dt.hour * 60 + end_dt.minute

    if start_m < work_start or end_m > work_end:
        return False

    break_start = _time_to_minutes(row.break_from)
    break_end = _time_to_minutes(row.break_to)
    if break_start is not None and break_end is not None and break_end > break_start:
        service_range = TimeRangeMinutes(start_minutes=start_m, end_minutes=end_m)
        break_range = TimeRangeMinutes(start_minutes=break_start, end_minutes=break_end)
        if _interval_overlaps(service_range, break_range):
            return False

    from app.master_time_blocks import master_interval_overlaps_time_block

    if master_interval_overlaps_time_block(
        db, master_id=master_id, d=d, start_m=start_m, end_m=end_m
    ):
        return False

    return True


def _write_audit_rows_for_master_schedule(
    db: Session,
    *,
    master_id: int,
    work_date: date,
    changed_by_user_id: int | None,
    changes: list[tuple[str, str | None, str | None]],
) -> None:
    """
    Упрощённая запись audit: master_id+work_date обязательны, поэтому
    пишем напрямую вместо универсального write_audit_rows (там нет work_date).
    """
    if not changes:
        return
    now = datetime.utcnow()
    for field_name, old_s, new_s in changes:
        db.add(
            MasterScheduleAuditLog(
                master_id=master_id,
                work_date=work_date,
                changed_at=now,
                changed_by_user_id=changed_by_user_id,
                field_name=audit_field_label(field_name),
                old_value=old_s,
                new_value=new_s,
            )
        )


def save_master_schedule_day(
    db: Session,
    *,
    master_id: int,
    work_date: date,
    status: MasterScheduleStatus,
    time_from: time | None,
    time_to: time | None,
    break_from: time | None,
    break_to: time | None,
    changed_by_user_id: int | None,
) -> MasterScheduleDay:
    """Upsert одного дня в график с минимальным audit (old/new по изменившимся полям)."""
    row = db.scalar(
        select(MasterScheduleDay).where(
            MasterScheduleDay.master_id == master_id, MasterScheduleDay.work_date == work_date
        )
    )
    if row is None:
        row = MasterScheduleDay(
            master_id=master_id,
            work_date=work_date,
            status=status,
            time_from=time_from,
            time_to=time_to,
            break_from=break_from if status == MasterScheduleStatus.WORKING else None,
            break_to=break_to if status == MasterScheduleStatus.WORKING else None,
            updated_by_user_id=changed_by_user_id,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        # Создание — audit всех полей как old=None.
        _write_audit_rows_for_master_schedule(
            db,
            master_id=master_id,
            work_date=work_date,
            changed_by_user_id=changed_by_user_id,
            changes=[
                ("status", None, status.value),
                ("time_from", None, str(time_from) if time_from else None),
                ("time_to", None, str(time_to) if time_to else None),
                ("break_from", None, str(break_from) if break_from else None),
                ("break_to", None, str(break_to) if break_to else None),
            ],
        )
        return row

    old_status = row.status
    old_time_from = row.time_from
    old_time_to = row.time_to
    old_break_from = row.break_from
    old_break_to = row.break_to

    row.status = status
    row.time_from = time_from if status == MasterScheduleStatus.WORKING else None
    row.time_to = time_to if status == MasterScheduleStatus.WORKING else None
    row.break_from = break_from if status == MasterScheduleStatus.WORKING else None
    row.break_to = break_to if status == MasterScheduleStatus.WORKING else None
    row.updated_by_user_id = changed_by_user_id
    row.updated_at = datetime.utcnow()

    # Audit: пишем только изменившиеся поля.
    changes: list[tuple[str, str | None, str | None]] = []
    if old_status != row.status:
        changes.append(("status", old_status.value if old_status else None, row.status.value if row.status else None))
    if old_time_from != row.time_from:
        changes.append(("time_from", str(old_time_from) if old_time_from else None, str(row.time_from) if row.time_from else None))
    if old_time_to != row.time_to:
        changes.append(("time_to", str(old_time_to) if old_time_to else None, str(row.time_to) if row.time_to else None))
    if old_break_from != row.break_from:
        changes.append(("break_from", str(old_break_from) if old_break_from else None, str(row.break_from) if row.break_from else None))
    if old_break_to != row.break_to:
        changes.append(("break_to", str(old_break_to) if old_break_to else None, str(row.break_to) if row.break_to else None))

    _write_audit_rows_for_master_schedule(
        db,
        master_id=master_id,
        work_date=work_date,
        changed_by_user_id=changed_by_user_id,
        changes=changes,
    )
    return row


def apply_weekday_bulk(
    db: Session,
    *,
    master_id: int,
    date_from: date,
    date_to: date,
    working_weekdays: set[int],
    time_from: time | None,
    time_to: time | None,
    break_from: time | None,
    break_to: time | None,
    changed_by_user_id: int | None,
) -> int:
    """
    Проставляет записи на диапазоне [date_from, date_to] включительно.

    Для дней вне working_weekdays создаёт DAY_OFF (а не no_data).
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    updated = 0
    d = date_from
    while d <= date_to:
        wd = int(d.weekday())  # Monday=0
        is_work = wd in working_weekdays
        status = MasterScheduleStatus.WORKING if is_work else MasterScheduleStatus.DAY_OFF
        save_master_schedule_day(
            db,
            master_id=master_id,
            work_date=d,
            status=status,
            time_from=time_from if is_work else None,
            time_to=time_to if is_work else None,
            break_from=break_from if is_work else None,
            break_to=break_to if is_work else None,
            changed_by_user_id=changed_by_user_id,
        )
        updated += 1
        d = d + timedelta(days=1)
    return updated


def apply_cyclic_bulk(
    db: Session,
    *,
    master_id: int,
    date_from: date,
    date_to: date,
    scheme: Literal["ALL_DAYS", "WEEKDAYS", "ODD_EVEN", "CUSTOM"],
    odd_even_mode: Literal["ODD", "EVEN"] = "ODD",
    custom_work_days: int,
    custom_day_off_days: int,
    time_from: time | None,
    time_to: time | None,
    break_from: time | None,
    break_to: time | None,
    changed_by_user_id: int | None,
) -> int:
    """
    Массовая циклическая настройка.

    В MVP поддерживаем:
    - ALL_DAYS: все дни working
    - WEEKDAYS: пн-пт working, сб/вс выходной
    - ODD_EVEN: alternation по индексу дня (чётные/нечётные с param "custom_work_days" не различаем в MVP)
    - CUSTOM: рабочие_дни_подряд через выходные_дни_подряд с чередованием, старт "рабочая пачка" с date_from.
    """
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    updated = 0
    d = date_from
    day_index = 0
    # Для CUSTOM требуется старт "чередования" с сегодняшнего дня.
    base_today = date.today()
    while d <= date_to:
        wd = int(d.weekday())  # Mon=0
        is_work = False

        if scheme == "ALL_DAYS":
            is_work = True
        elif scheme == "WEEKDAYS":
            is_work = wd <= 4
        elif scheme == "ODD_EVEN":
            # day_index: 0 => day #1 in selected period
            # ODD: 1,3,5,... are working; EVEN: 2,4,6,... are working
            is_odd = (day_index % 2) == 0
            is_work = is_odd if (odd_even_mode or "ODD") == "ODD" else (not is_odd)
        elif scheme == "CUSTOM":
            work_n = max(1, int(custom_work_days or 1))
            off_n = max(1, int(custom_day_off_days or 1))
            cycle = work_n + off_n
            pos = (d - base_today).days % cycle
            is_work = pos < work_n

        status = MasterScheduleStatus.WORKING if is_work else MasterScheduleStatus.DAY_OFF
        save_master_schedule_day(
            db,
            master_id=master_id,
            work_date=d,
            status=status,
            time_from=time_from if is_work else None,
            time_to=time_to if is_work else None,
            break_from=break_from if is_work else None,
            break_to=break_to if is_work else None,
            changed_by_user_id=changed_by_user_id,
        )

        updated += 1
        d = d + timedelta(days=1)
        day_index += 1

    return updated

