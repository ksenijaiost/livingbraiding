"""Планы работ: доменная логика и подписи."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import HourlyWorkEntry, WorkForInventory, WorkKind, WorkPlan, WorkPlanStatus, WorkPlanType, User, UserRole
from app.display_time import get_display_timezone
from app.master_schedule import TimeRangeMinutes, _interval_overlaps, is_master_available_for_interval
from app.time_utils import utcnow_naive
from app.forms_parse import parse_bool
from app.user_roles import select_users_with_any_role

# Несколько мастеров в одном POST дают отдельные строки WorkPlan с одним created_at.
_SIBLING_CREATED_AT_WINDOW = timedelta(seconds=10)

WORK_KIND_LABELS: dict[WorkKind, str] = {
    WorkKind.KIT: "Комплект/Заготовки (поштучно)",
    WorkKind.MIX: "Смешка",
    WorkKind.RUBBER: "Хвосты/резинки",
    WorkKind.KIT_CORRECTION: "Коррекция комплекта",
    WorkKind.OTHER: "Другое",
    WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию волос (заглушка)",
}


def list_masters_for_work_plan_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_any_role(UserRole.MASTER, UserRole.HELPER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )


def work_kind_label(kind: WorkKind | str | None) -> str:
    if kind is None:
        return "—"
    if isinstance(kind, WorkKind):
        return WORK_KIND_LABELS.get(kind, kind.value)
    try:
        return WORK_KIND_LABELS.get(WorkKind(str(kind)), str(kind))
    except ValueError:
        return str(kind)


def work_plan_status_label(status: WorkPlanStatus | str) -> str:
    s = status.value if isinstance(status, WorkPlanStatus) else str(status)
    return {
        WorkPlanStatus.PLANNED.value: "Запланировано",
        WorkPlanStatus.COMPLETED.value: "Выполнено",
        WorkPlanStatus.CANCELLED.value: "Отменено",
    }.get(s, s)


def work_plan_status_emoji(status: WorkPlanStatus | str) -> str:
    """Короткий статус для узких экранов списка планов."""
    s = status.value if isinstance(status, WorkPlanStatus) else str(status)
    return {
        WorkPlanStatus.PLANNED.value: "⌛",
        WorkPlanStatus.COMPLETED.value: "✅",
        WorkPlanStatus.CANCELLED.value: "❌",
    }.get(s, "·")


def work_plan_type_label(plan_type: WorkPlanType | str) -> str:
    t = plan_type.value if isinstance(plan_type, WorkPlanType) else str(plan_type)
    return {
        WorkPlanType.WORK_PRODUCT.value: "Работа с товаром",
        WorkPlanType.HOURLY.value: "Почасовая работа",
    }.get(t, t)


def work_plan_type_display(plan: WorkPlan) -> str:
    if plan.plan_type == WorkPlanType.HOURLY:
        return work_plan_type_label(WorkPlanType.HOURLY)
    base = work_plan_type_label(WorkPlanType.WORK_PRODUCT)
    if plan.work_kind:
        return f"{base}: {work_kind_label(plan.work_kind)}"
    return base


def work_plan_is_open(plan: WorkPlan) -> bool:
    return plan.status == WorkPlanStatus.PLANNED


def _utc_naive_to_local(dt: datetime, tz_name: str) -> datetime:
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def _local_naive_to_utc_naive(dt: datetime, tz_name: str) -> datetime:
    local = dt.replace(tzinfo=ZoneInfo(tz_name))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def parse_planned_datetime(local_day: date, time_raw: str, tz_name: str) -> datetime:
    parts = (time_raw or "").strip().replace(".", ":").split(":")
    if len(parts) < 2:
        raise ValueError("Укажите время начала.")
    hh = int(parts[0])
    mm = int(parts[1]) if len(parts) > 1 else 0
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("Некорректное время.")
    local_dt = datetime.combine(local_day, time(hh, mm))
    return _local_naive_to_utc_naive(local_dt, tz_name).replace(second=0, microsecond=0)


def planned_local_datetime(plan: WorkPlan, tz_name: str) -> datetime:
    return _utc_naive_to_local(plan.planned_date, tz_name)


def planned_end_utc(plan: WorkPlan) -> datetime:
    return plan.planned_date + timedelta(minutes=max(0, int(plan.duration_minutes or 0)))


def planned_end_local(plan: WorkPlan, tz_name: str) -> datetime:
    return _utc_naive_to_local(planned_end_utc(plan), tz_name)


def parse_duration_minutes(duration_h: int, duration_m: int) -> int:
    total = max(0, int(duration_h or 0)) * 60 + max(0, int(duration_m or 0))
    if total <= 0:
        raise ValueError("Укажите длительность.")
    return total


def _intervals_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    return not (end_a <= start_b or end_b <= start_a)


def list_planned_work_plans_for_master_on_day(
    db: Session,
    *,
    master_id: int,
    day_start_utc: datetime,
    day_end_utc: datetime,
    exclude_plan_id: int | None = None,
) -> list[WorkPlan]:
    stmt = (
        select(WorkPlan)
        .where(
            WorkPlan.master_id == master_id,
            WorkPlan.status == WorkPlanStatus.PLANNED,
            WorkPlan.planned_date >= day_start_utc,
            WorkPlan.planned_date < day_end_utc,
        )
        .order_by(WorkPlan.planned_date.asc(), WorkPlan.id.asc())
    )
    if exclude_plan_id:
        stmt = stmt.where(WorkPlan.id != exclude_plan_id)
    return list(db.scalars(stmt).all())


def master_has_work_plan_conflict(
    db: Session,
    *,
    master_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_plan_id: int | None = None,
) -> bool:
    if end_dt <= start_dt or start_dt.date() != end_dt.date():
        return True
    day_start = datetime.combine(start_dt.date(), time.min)
    day_end = day_start + timedelta(days=1)
    for plan in list_planned_work_plans_for_master_on_day(
        db,
        master_id=master_id,
        day_start_utc=day_start,
        day_end_utc=day_end,
        exclude_plan_id=exclude_plan_id,
    ):
        if _intervals_overlap(start_dt, end_dt, plan.planned_date, planned_end_utc(plan)):
            return True
    return False


def master_interval_overlaps_occupancy(
    db: Session,
    *,
    master_id: int,
    d: date,
    start_m: int,
    end_m: int,
    exclude_plan_id: int | None = None,
    exclude_booking_id: int | None = None,
) -> bool:
    """Пересечение с бронями/консультациями/планами (segments) и блоками «Занять время»."""
    from app.calendar_occupancy import build_occupancy_for_day

    occ = build_occupancy_for_day(
        db,
        day=d,
        hour_from=0,
        hour_to=24,
        exclude_work_plan_id=exclude_plan_id,
        exclude_booking_id=exclude_booking_id,
    )
    service_range = TimeRangeMinutes(start_minutes=start_m, end_minutes=end_m)
    for seg in occ.get("segments") or []:
        if int(seg.get("master_id") or 0) != int(master_id):
            continue
        if exclude_plan_id and int(seg.get("work_plan_id") or 0) == int(exclude_plan_id):
            continue
        if exclude_booking_id and int(seg.get("booking_id") or 0) == int(exclude_booking_id):
            continue
        seg_range = TimeRangeMinutes(
            start_minutes=int(seg["start_minutes"]),
            end_minutes=int(seg["end_minutes"]),
        )
        if _interval_overlaps(service_range, seg_range):
            return True
    for seg in occ.get("block_segments") or []:
        if int(seg.get("master_id") or 0) != int(master_id):
            continue
        seg_range = TimeRangeMinutes(
            start_minutes=int(seg["start_minutes"]),
            end_minutes=int(seg["end_minutes"]),
        )
        if _interval_overlaps(service_range, seg_range):
            return True
    return False


def is_master_available_for_work_plan(
    db: Session,
    *,
    master_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_plan_id: int | None = None,
) -> bool:
    if not is_master_available_for_interval(db, master_id=master_id, start_dt=start_dt, end_dt=end_dt):
        return False
    if master_has_work_plan_conflict(
        db, master_id=master_id, start_dt=start_dt, end_dt=end_dt, exclude_plan_id=exclude_plan_id
    ):
        return False
    start_m = start_dt.hour * 60 + start_dt.minute
    end_m = end_dt.hour * 60 + end_dt.minute
    if master_interval_overlaps_occupancy(
        db,
        master_id=master_id,
        d=start_dt.date(),
        start_m=start_m,
        end_m=end_m,
        exclude_plan_id=exclude_plan_id,
    ):
        return False
    return True


def is_master_available_for_booking(
    db: Session,
    *,
    master_id: int,
    start_dt: datetime,
    end_dt: datetime,
    exclude_booking_id: int | None = None,
) -> bool:
    """
    Доступность мастера для брони/консультации:
    график смены + перерыв + «Занять время» + другие брони/консультации + планы работ.
    """
    if not is_master_available_for_interval(db, master_id=master_id, start_dt=start_dt, end_dt=end_dt):
        return False
    start_m = start_dt.hour * 60 + start_dt.minute
    end_m = end_dt.hour * 60 + end_dt.minute
    if master_interval_overlaps_occupancy(
        db,
        master_id=master_id,
        d=start_dt.date(),
        start_m=start_m,
        end_m=end_m,
        exclude_booking_id=exclude_booking_id,
    ):
        return False
    return True


def validate_work_plan_interval(
    db: Session,
    *,
    master_id: int,
    start_utc: datetime,
    duration_minutes: int,
    exclude_plan_id: int | None = None,
) -> str | None:
    tz = get_display_timezone(db)
    start_local = _utc_naive_to_local(start_utc, tz)
    end_local = start_local + timedelta(minutes=max(1, int(duration_minutes)))
    if not is_master_available_for_work_plan(
        db,
        master_id=master_id,
        start_dt=start_local,
        end_dt=end_local,
        exclude_plan_id=exclude_plan_id,
    ):
        return "Мастер недоступен на выбранное время (график или занятость)."
    return None


def _plan_master_name(plan: WorkPlan) -> str:
    master = getattr(plan, "master", None)
    if master is None:
        return str(plan.master_id)
    return str(master.display_name or master.username or plan.master_id).strip()


def sibling_work_plans(
    db: Session,
    plan: WorkPlan,
    *,
    include_self: bool = True,
    statuses: tuple[WorkPlanStatus, ...] | None = None,
) -> list[WorkPlan]:
    """Другие строки того же «мульти-мастер» плана (отдельная карточка на мастера)."""
    if plan.created_at is None:
        return [plan] if include_self else []
    lo = plan.created_at - _SIBLING_CREATED_AT_WINDOW
    hi = plan.created_at + _SIBLING_CREATED_AT_WINDOW
    stmt = (
        select(WorkPlan)
        .options(selectinload(WorkPlan.master))
        .where(
            WorkPlan.created_by_user_id == plan.created_by_user_id,
            WorkPlan.plan_type == plan.plan_type,
            WorkPlan.created_at >= lo,
            WorkPlan.created_at <= hi,
        )
        .order_by(WorkPlan.id.asc())
    )
    if plan.work_kind is None:
        stmt = stmt.where(WorkPlan.work_kind.is_(None))
    else:
        stmt = stmt.where(WorkPlan.work_kind == plan.work_kind)
    comment = (plan.comment or "").strip()
    if comment:
        stmt = stmt.where(WorkPlan.comment == plan.comment)
    else:
        stmt = stmt.where(or_(WorkPlan.comment.is_(None), WorkPlan.comment == ""))
    if statuses:
        stmt = stmt.where(WorkPlan.status.in_(statuses))
    if not include_self:
        stmt = stmt.where(WorkPlan.id != plan.id)
    rows = list(db.scalars(stmt).all())
    if include_self and not any(int(p.id) == int(plan.id) for p in rows):
        rows = [plan] + rows
    return rows


def planned_masters_for_work_plan(db: Session, plan_id: int) -> list[dict[str, Any]]:
    """Мастера открытых планов той же пачки (для формы работы и подтверждения)."""
    plan = db.get(WorkPlan, int(plan_id))
    if plan is None:
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for p in sibling_work_plans(db, plan, include_self=True, statuses=(WorkPlanStatus.PLANNED,)):
        mid = int(p.master_id)
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "name": _plan_master_name(p)})
    return out


def missing_planned_masters_confirm_text(names: list[str]) -> str:
    clean = [n.strip() for n in names if str(n).strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return (
            f"Вы сохраняете работу, но на неё был запланирован мастер {clean[0]}. "
            "Точно сохранить без него?"
        )
    joined = ", ".join(clean)
    return (
        f"Вы сохраняете работу, но на неё были запланированы мастера {joined}. "
        "Точно сохранить без них?"
    )


def missing_planned_master_names(
    db: Session,
    plan_id: int,
    work_master_ids: set[int],
) -> list[str]:
    present = {int(x) for x in work_master_ids if int(x) > 0}
    names: list[str] = []
    for row in planned_masters_for_work_plan(db, plan_id):
        if int(row["id"]) not in present:
            names.append(str(row["name"]))
    return names


def require_work_plan_missing_masters_ack(
    db: Session,
    plan_id: int,
    work_master_ids: set[int],
    form_ack: object,
) -> None:
    names = missing_planned_master_names(db, plan_id, work_master_ids)
    if not names:
        return
    if parse_bool(form_ack):
        return
    raise ValueError(missing_planned_masters_confirm_text(names))


def complete_work_plan_from_work(db: Session, plan_id: int, work_id: int) -> None:
    plan = db.get(WorkPlan, plan_id)
    if plan is None:
        return
    work = db.get(WorkForInventory, work_id)
    if work is not None and work.work_plan_id is None:
        work.work_plan_id = plan.id
    now = utcnow_naive()
    to_close = sibling_work_plans(db, plan, include_self=True, statuses=(WorkPlanStatus.PLANNED,))
    for p in to_close:
        if not work_plan_is_open(p):
            continue
        p.status = WorkPlanStatus.COMPLETED
        p.completed_at = now
        p.updated_at = now


def complete_work_plan_from_hourly_work(db: Session, plan_id: int, entry_id: int) -> None:
    plan = db.get(WorkPlan, plan_id)
    if plan is None or not work_plan_is_open(plan):
        return
    entry = db.get(HourlyWorkEntry, entry_id)
    if entry is not None and entry.work_plan_id is None:
        entry.work_plan_id = plan.id
    plan.status = WorkPlanStatus.COMPLETED
    plan.completed_at = utcnow_naive()
    plan.updated_at = utcnow_naive()


def validate_work_plan_for_hourly_entry(
    db: Session,
    *,
    plan_id: int,
    master_user_id: int,
) -> str | None:
    plan = db.get(WorkPlan, int(plan_id))
    if plan is None:
        return "План работ не найден."
    if not work_plan_is_open(plan):
        return "План работ уже закрыт."
    if plan.plan_type != WorkPlanType.HOURLY:
        return "План не предназначен для почасовой работы."
    if int(plan.master_id) != int(master_user_id):
        return "Почасовую работу можно создать только для мастера плана."
    if linked_hourly_work_for_plan(db, plan.id) is not None:
        return "По этому плану уже создана почасовая работа."
    return None


def work_plan_work_new_query_params(db: Session, plan: WorkPlan) -> dict[str, str]:
    tz = get_display_timezone(db)
    q: dict[str, str] = {"work_plan_id": str(plan.id)}
    local = planned_local_datetime(plan, tz)
    q["performed_date"] = local.date().isoformat()
    if plan.plan_type == WorkPlanType.WORK_PRODUCT and plan.work_kind:
        q["kind"] = plan.work_kind.value
    parts = [f"план работ #{plan.id}"]
    if plan.comment:
        parts.append(str(plan.comment).strip()[:400])
    q["comment"] = "\n".join(parts)[:900]
    return q


def work_plan_hourly_new_query_params(db: Session, plan: WorkPlan) -> dict[str, str]:
    tz = get_display_timezone(db)
    q: dict[str, str] = {"work_plan_id": str(plan.id)}
    local = planned_local_datetime(plan, tz)
    q["performed_date"] = local.date().isoformat()
    q["duration_h"] = str(plan.duration_minutes // 60)
    q["duration_m"] = str(plan.duration_minutes % 60)
    q["master_id"] = str(plan.master_id)
    parts = [f"план работ #{plan.id}"]
    if plan.comment:
        parts.append(str(plan.comment).strip()[:400])
    q["comment"] = "\n".join(parts)[:900]
    return q


def linked_work_for_plan(db: Session, plan_id: int) -> WorkForInventory | None:
    direct = db.scalar(
        select(WorkForInventory)
        .where(WorkForInventory.work_plan_id == plan_id, WorkForInventory.is_voided.is_(False))
        .order_by(WorkForInventory.id.desc())
        .limit(1)
    )
    if direct is not None:
        return direct
    plan = db.get(WorkPlan, int(plan_id))
    if plan is None:
        return None
    sibling_ids = [int(p.id) for p in sibling_work_plans(db, plan, include_self=True)]
    if not sibling_ids:
        return None
    return db.scalar(
        select(WorkForInventory)
        .where(
            WorkForInventory.work_plan_id.in_(sibling_ids),
            WorkForInventory.is_voided.is_(False),
        )
        .order_by(WorkForInventory.id.desc())
        .limit(1)
    )


def linked_hourly_work_for_plan(db: Session, plan_id: int) -> HourlyWorkEntry | None:
    return db.scalar(
        select(HourlyWorkEntry)
        .where(
            HourlyWorkEntry.work_plan_id == plan_id,
            HourlyWorkEntry.is_voided.is_(False),
        )
        .order_by(HourlyWorkEntry.id.desc())
        .limit(1)
    )
