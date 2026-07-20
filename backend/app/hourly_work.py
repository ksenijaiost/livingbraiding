"""Почасовая работа мастера: парсинг формы и сохранение."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import HourlyWorkEntry, User, UserRole
from app.forms_parse import parse_date_iso, parse_float, parse_int
from app.hourly_help import format_hourly_help_duration
from app.payroll_fund import post_hourly_work_accruals
from app.user_roles import select_users_with_role, user_has_role
from app.visit_edit_policy import ensure_event_date_in_open_payroll_period


def list_masters_for_hourly_work_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_role(UserRole.MASTER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )


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

    entry = HourlyWorkEntry(
        performed_date=datetime.combine(performed_day, datetime.min.time()),
        duration_minutes=int(duration_minutes),
        amount=float(amount),
        comment=comment,
        master_user_id=int(master_id),
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
    entry.created_by_user_id = int(created_by_user_id)
    db.add(entry)
    db.flush()
    post_hourly_work_accruals(db, entry, created_by_user_id)
    db.commit()
    db.refresh(entry)
    return entry


def duration_display(minutes: int) -> str:
    h = int(minutes or 0) // 60
    m = int(minutes or 0) % 60
    return format_hourly_help_duration(h, m)
