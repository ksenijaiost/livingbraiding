"""Правила редактирования визита (окно дней с создания, роли, участие мастера)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.time_utils import utcnow_naive
from app.auth import AuthUser
from app.db.models import PayrollPeriod, Setting, UserRole, Visit, VisitMaster, VisitService, VisitServiceMaster
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
        raise ValueError("Нет открытого периода ЗП. Суперадмин должен открыть текущий период.")

    if event_at < open_p.date_from:
        raise ValueError("Дата раньше начала текущего открытого периода ЗП, создание невозможно.")


def user_participates_in_visit(db: Session, visit_id: int, user_id: int) -> bool:
    """Мастер участвует в визите: VisitMaster, VisitServiceMaster или mix_bonus."""
    if db.scalar(
        select(VisitMaster.id).where(VisitMaster.visit_id == visit_id, VisitMaster.master_id == user_id).limit(1)
    ):
        return True
    if db.scalar(
        select(VisitServiceMaster.id)
        .join(VisitService, VisitService.id == VisitServiceMaster.visit_service_id)
        .where(
            VisitService.visit_id == visit_id,
            VisitService.is_cancelled.is_(False),
            VisitServiceMaster.master_id == user_id,
        )
        .limit(1)
    ):
        return True
    visit = db.get(Visit, visit_id)
    if visit and visit.mix_bonus_master_id == user_id:
        return True
    if db.scalar(
        select(VisitService.id).where(
            VisitService.visit_id == visit_id,
            VisitService.is_cancelled.is_(False),
            VisitService.mix_bonus_master_id == user_id,
        ).limit(1)
    ):
        return True
    return False


@dataclass(frozen=True)
class VisitEditPolicy:
    can_edit: bool
    message_when_blocked: str


@dataclass(frozen=True)
class VisitClientChangePolicy:
    """can_change: показать форму смены клиента; super_outside_window — legacy, всегда False."""

    can_change: bool
    message_when_blocked: str
    super_outside_window: bool


def _edit_window_block_message(days: int) -> str:
    return (
        f"Редактирование доступно только в течение {days} дн. с даты создания визита "
        "(параметр «Окно редактирования» в настройках студии)."
    )


def visit_edit_policy(visit: Visit, user: AuthUser, db: Session) -> VisitEditPolicy:
    if visit.is_cancelled:
        return VisitEditPolicy(False, "Визит отменён — редактирование запрещено.")
    if is_in_closed_payroll_period(db, visit.created_at):
        return VisitEditPolicy(
            False,
            "Визит относится к закрытому периоду ЗП — редактирование и аннулирование запрещено.",
        )

    if UserRole.ADMIN_SUPER in user.roles or UserRole.TECHSPEC in user.roles:
        return VisitEditPolicy(True, "")

    days = edit_window_days(db)
    inside = within_edit_window(visit, days)

    if user.role == UserRole.ADMIN:
        if inside:
            return VisitEditPolicy(True, "")
        return VisitEditPolicy(False, _edit_window_block_message(days))

    if user.role == UserRole.MASTER:
        if not inside:
            return VisitEditPolicy(False, _edit_window_block_message(days))
        if not user_participates_in_visit(db, visit.id, user.id):
            return VisitEditPolicy(False, "Редактировать визит может только мастер, участвующий в этом визите.")
        return VisitEditPolicy(True, "")

    return VisitEditPolicy(False, "Недостаточно прав.")


def visit_client_change_policy(visit: Visit, user: AuthUser, db: Session) -> VisitClientChangePolicy:
    ep = visit_edit_policy(visit, user, db)
    return VisitClientChangePolicy(
        can_change=ep.can_edit,
        message_when_blocked=ep.message_when_blocked,
        super_outside_window=False,
    )
