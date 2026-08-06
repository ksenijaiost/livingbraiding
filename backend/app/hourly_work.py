"""Почасовая работа мастера: парсинг формы и сохранение."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import HourlyWorkEntry, User, UserRole, WorkPlan, WorkPlanStatus
from app.forms_parse import parse_date_iso, parse_float, parse_int
from app.hourly_help import format_hourly_help_duration
from app.payroll_fund import (
    PayrollFundSourceKind,
    post_hourly_work_accruals,
    replace_hourly_work_accruals,
    storno_source_accruals,
)
from app.time_utils import utcnow_naive
from app.user_roles import select_users_with_role, user_has_role
from app.visit_edit_policy import ensure_event_date_in_open_payroll_period
from app.work_plan import complete_work_plan_from_hourly_work, linked_hourly_work_for_plan, validate_work_plan_for_hourly_entry


def list_masters_for_hourly_work_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_role(UserRole.MASTER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )


def can_access_hourly_work_entry(
    entry: HourlyWorkEntry,
    *,
    current_user_id: int,
    is_admin: bool,
) -> bool:
    if is_admin:
        return True
    return int(entry.master_user_id) == int(current_user_id)


def entry_to_form_prefill(entry: HourlyWorkEntry) -> dict[str, str]:
    mins = int(entry.duration_minutes or 0)
    fp = {
        "performed_date": entry.performed_date.date().isoformat() if entry.performed_date else "",
        "duration_h": str(mins // 60),
        "duration_m": str(mins % 60),
        "amount": str(int(entry.amount) if float(entry.amount).is_integer() else entry.amount),
        "comment": entry.comment or "",
        "master_id": str(entry.master_user_id),
    }
    if entry.work_plan_id:
        fp["work_plan_id"] = str(entry.work_plan_id)
    return fp


def _form_str(form: Any, name: str) -> str:
    raw = form.get(name)
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode().strip()
    return str(raw).strip()


def parse_hourly_work_form(
    form: Any,
    *,
    current_user_id: int,
    is_admin: bool,
) -> tuple[HourlyWorkEntry | None, str | None]:
    """Вернуть (entry draft без id, error)."""
    if is_admin:
        master_id = parse_int(_form_str(form, "master_id"), min=1, field_name="master_id")
    else:
        master_id = int(current_user_id)

    pd_raw = _form_str(form, "performed_date")
    if not pd_raw:
        return None, "Укажите дату."
    try:
        performed_day = parse_date_iso(pd_raw, field_name="performed_date")
    except ValueError:
        return None, "Некорректная дата."

    try:
        hours = parse_int(_form_str(form, "duration_h"), min=0, field_name="duration_h", default=0)
        minutes = parse_int(_form_str(form, "duration_m"), min=0, field_name="duration_m", default=0)
    except ValueError as exc:
        return None, str(exc)
    if minutes >= 60:
        return None, "Минуты должны быть меньше 60."
    duration_minutes = hours * 60 + minutes
    if duration_minutes <= 0:
        return None, "Укажите длительность (часы или минуты)."

    amount_raw = _form_str(form, "amount")
    if not amount_raw:
        return None, "Укажите сумму."
    try:
        amount = float(parse_float(amount_raw, min=0.0, field_name="amount"))
    except ValueError:
        return None, "Сумма должна быть числом."
    if amount <= 0:
        return None, "Сумма должна быть больше нуля."

    comment = _form_str(form, "comment") or None

    work_plan_id: int | None = None
    wp_raw = _form_str(form, "work_plan_id")
    if wp_raw:
        try:
            work_plan_id = parse_int(wp_raw, min=1, field_name="work_plan_id")
        except ValueError:
            return None, "Некорректный план работ."

    entry = HourlyWorkEntry(
        performed_date=datetime.combine(performed_day, datetime.min.time()),
        duration_minutes=int(duration_minutes),
        amount=float(amount),
        comment=comment,
        master_user_id=int(master_id),
        work_plan_id=work_plan_id,
    )
    return entry, None


def validate_hourly_work_master(db: Session, master_id: int) -> str | None:
    u = db.get(User, int(master_id))
    if not u or not u.is_active:
        return "Мастер не найден или отключён."
    if not user_has_role(db, int(master_id), UserRole.MASTER):
        return "ЗП может получить только мастер."
    return None


def create_hourly_work_entry(
    db: Session,
    entry: HourlyWorkEntry,
    *,
    created_by_user_id: int,
) -> HourlyWorkEntry:
    ensure_event_date_in_open_payroll_period(db, entry.performed_date)
    err = validate_hourly_work_master(db, int(entry.master_user_id))
    if err:
        raise ValueError(err)
    if entry.work_plan_id is not None:
        wp_err = validate_work_plan_for_hourly_entry(
            db,
            plan_id=int(entry.work_plan_id),
            master_user_id=int(entry.master_user_id),
        )
        if wp_err:
            raise ValueError(wp_err)
    entry.created_by_user_id = int(created_by_user_id)
    db.add(entry)
    db.flush()
    post_hourly_work_accruals(db, entry, created_by_user_id)
    if entry.work_plan_id is not None:
        complete_work_plan_from_hourly_work(db, int(entry.work_plan_id), int(entry.id))
    db.commit()
    db.refresh(entry)
    return entry


def update_hourly_work_entry(
    db: Session,
    entry: HourlyWorkEntry,
    draft: HourlyWorkEntry,
    *,
    updated_by_user_id: int,
    is_admin: bool,
) -> HourlyWorkEntry:
    """Обновить запись и пересчитать проводки ЗП/фонда студии."""
    if entry.is_voided:
        raise ValueError("Запись аннулирована — редактирование недоступно.")
    ensure_event_date_in_open_payroll_period(db, draft.performed_date)
    master_id = int(draft.master_user_id) if is_admin else int(entry.master_user_id)
    err = validate_hourly_work_master(db, master_id)
    if err:
        raise ValueError(err)

    entry.performed_date = draft.performed_date
    entry.duration_minutes = int(draft.duration_minutes)
    entry.amount = float(draft.amount)
    entry.comment = draft.comment
    entry.master_user_id = master_id
    # Связь с планом не меняем при правке.
    db.flush()
    replace_hourly_work_accruals(db, entry, updated_by_user_id)
    db.commit()
    db.refresh(entry)
    return entry


def void_hourly_work_entry(
    db: Session,
    entry: HourlyWorkEntry,
    *,
    voided_by_user_id: int,
) -> HourlyWorkEntry:
    """Аннулировать почасовую работу: сторно ЗП, при необходимости открыть план работ."""
    if entry.is_voided:
        raise ValueError("Запись уже аннулирована.")
    ensure_event_date_in_open_payroll_period(db, entry.performed_date)
    storno_source_accruals(
        db,
        PayrollFundSourceKind.HOURLY_WORK,
        int(entry.id),
        voided_by_user_id,
    )
    entry.is_voided = True
    entry.voided_at = utcnow_naive()
    entry.voided_by_user_id = int(voided_by_user_id)
    db.flush()
    if entry.work_plan_id is not None:
        plan = db.get(WorkPlan, int(entry.work_plan_id))
        if plan is not None and plan.status == WorkPlanStatus.COMPLETED:
            still_linked = linked_hourly_work_for_plan(db, int(plan.id))
            if still_linked is None:
                plan.status = WorkPlanStatus.PLANNED
                plan.completed_at = None
                plan.updated_at = utcnow_naive()
    db.commit()
    db.refresh(entry)
    return entry


def duration_display(minutes: int) -> str:
    h = int(minutes or 0) // 60
    m = int(minutes or 0) % 60
    return format_hourly_help_duration(h, m)
