"""Правила редактирования визита (окно дней с создания); позже расширение под роли/этап 5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.time_utils import utcnow_naive
from app.auth import AuthUser
from app.db.models import PayrollPeriod, Setting, UserRole, Visit
from app.setting_keys import EDIT_WINDOW_DAYS


def edit_window_days(db: Session) -> int:
    row = db.get(Setting, EDIT_WINDOW_DAYS)
    if not row or not (row.value or "").strip():
        return 2
    try:
        return max(0, int(row.value.strip()))
    except ValueError:
        return 2


def within_edit_window(visit: Visit, days: int, *, now: datetime | None = None) -> bool:
    if days <= 0:
        return False
    now = now or utcnow_naive()
    return visit.created_at + timedelta(days=days) >= now


def is_in_closed_payroll_period(db: Session, created_at: datetime) -> bool:
    """
    True если дата создания записи попадает в закрытый период ЗП.
    В этом случае блокируем редактирование (денежные поля и всё, что влияет на расчёты).
    """
    p = db.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.closed_at.is_not(None),
            PayrollPeriod.date_from <= created_at,
            PayrollPeriod.date_to >= created_at,
        )
    )
    return p is not None


def ensure_event_date_in_open_payroll_period(db: Session, event_at: datetime) -> None:
    """
    Запрещаем создание/перенос денежных событий в закрытый период ЗП.

    Правило:
    - если дата попадает в закрытый период → ошибка
    - иначе должна быть не раньше текущего открытого периода (от date_from и дальше)
    """
    closed = db.scalar(
        select(PayrollPeriod.id).where(
            PayrollPeriod.closed_at.is_not(None),
            PayrollPeriod.date_from <= event_at,
            PayrollPeriod.date_to >= event_at,
        )
    )
    if closed is not None:
        raise ValueError("Дата попадает в закрытый период ЗП, создание невозможно.")

    open_p = db.scalar(
        select(PayrollPeriod)
        .where(PayrollPeriod.closed_at.is_(None))
        .order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc())
        .limit(1)
    )
    if open_p is None:
        # В MVP без периода лучше не блокировать совсем, но по требованиям — ошибка.
        raise ValueError("Нет открытого периода ЗП. Суперадмин должен открыть текущий период.")

    if event_at < open_p.date_from:
        raise ValueError("Дата раньше начала текущего открытого периода ЗП, создание невозможно.")


@dataclass(frozen=True)
class VisitClientChangePolicy:
    """can_change: показать форму смены клиента; super_outside_window — нужно подтверждение при POST."""

    can_change: bool
    message_when_blocked: str
    super_outside_window: bool


def visit_client_change_policy(visit: Visit, user: AuthUser, db: Session) -> VisitClientChangePolicy:
    if is_in_closed_payroll_period(db, visit.created_at):
        return VisitClientChangePolicy(
            can_change=False,
            message_when_blocked="Визит относится к закрытому периоду ЗП — редактирование и аннулирование запрещено.",
            super_outside_window=False,
        )
    days = edit_window_days(db)
    inside = within_edit_window(visit, days)

    if UserRole.ADMIN_SUPER in user.roles:
        return VisitClientChangePolicy(
            can_change=True,
            message_when_blocked="",
            super_outside_window=not inside,
        )
    if user.role in (UserRole.ADMIN, UserRole.MASTER):
        if inside:
            return VisitClientChangePolicy(True, "", False)
        msg = (
            f"Смена клиента доступна только в течение {days} дн. с даты создания визита "
            "(параметр «Окно редактирования» в настройках студии)."
        )
        return VisitClientChangePolicy(False, msg, False)
    return VisitClientChangePolicy(False, "Недостаточно прав.", False)
