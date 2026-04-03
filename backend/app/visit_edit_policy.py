"""Правила редактирования визита (окно дней с создания); позже расширение под роли/этап 5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.auth import AuthUser
from app.db.models import Setting, UserRole, Visit


def edit_window_days(db: Session) -> int:
    row = db.get(Setting, "edit_window_days")
    if not row or not (row.value or "").strip():
        return 2
    try:
        return max(0, int(row.value.strip()))
    except ValueError:
        return 2


def within_edit_window(visit: Visit, days: int, *, now: datetime | None = None) -> bool:
    if days <= 0:
        return False
    now = now or datetime.utcnow()
    return visit.created_at + timedelta(days=days) >= now


@dataclass(frozen=True)
class VisitClientChangePolicy:
    """can_change: показать форму смены клиента; super_outside_window — нужно подтверждение при POST."""

    can_change: bool
    message_when_blocked: str
    super_outside_window: bool


def visit_client_change_policy(visit: Visit, user: AuthUser, db: Session) -> VisitClientChangePolicy:
    days = edit_window_days(db)
    inside = within_edit_window(visit, days)

    if user.role == UserRole.ADMIN_SUPER:
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
