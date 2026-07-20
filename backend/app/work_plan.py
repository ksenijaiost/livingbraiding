"""Планы работ: доменная логика и подписи."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import WorkForInventory, WorkKind, WorkPlan, WorkPlanStatus, WorkPlanType, User, UserRole
from app.display_time import get_display_timezone
from app.master_schedule import TimeRangeMinutes, _interval_overlaps, is_master_available_for_interval
from app.time_utils import utcnow_naive
from app.user_roles import select_users_with_role

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
            select_users_with_role(UserRole.MASTER)
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
) -> bool:
    from app.calendar_occupancy import build_occupancy_for_day

    occ = build_occupancy_for_day(db, day=d, hour_from=0, hour_to=24, exclude_work_plan_id=exclude_plan_id)
    service_range = TimeRangeMinutes(start_minutes=start_m, end_minutes=end_m)
    for seg in occ.get("segments") or []:
        if int(seg.get("master_id") or 0) != int(master_id):
            continue
        if exclude_plan_id and int(seg.get("work_plan_id") or 0) == int(exclude_plan_id):
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


def complete_work_plan_from_work(db: Session, plan_id: int, work_id: int) -> None:
    plan = db.get(WorkPlan, plan_id)
    if plan is None or not work_plan_is_open(plan):
        return
    work = db.get(WorkForInventory, work_id)
    if work is not None and work.work_plan_id is None:
        work.work_plan_id = plan.id
    plan.status = WorkPlanStatus.COMPLETED
    plan.completed_at = utcnow_naive()
    plan.updated_at = utcnow_naive()


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


def linked_work_for_plan(db: Session, plan_id: int) -> WorkForInventory | None:
    return db.scalar(
        select(WorkForInventory)
        .where(WorkForInventory.work_plan_id == plan_id, WorkForInventory.is_voided.is_(False))
        .order_by(WorkForInventory.id.desc())
        .limit(1)
    )
