"""Настройки отображения календаря (часы сетки занятости)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Setting
from app.setting_keys import CALENDAR_DISPLAY_HOUR_FROM, CALENDAR_DISPLAY_HOUR_TO

DEFAULT_CALENDAR_HOUR_FROM = 9
DEFAULT_CALENDAR_HOUR_TO = 21


def get_calendar_display_hours(db: Session) -> tuple[int, int]:
    """Часы [from, to) для сетки занятости: to — исключающая граница (21 → последняя метка 20:00)."""
    h_from = _read_hour_setting(db, CALENDAR_DISPLAY_HOUR_FROM, DEFAULT_CALENDAR_HOUR_FROM)
    h_to = _read_hour_setting(db, CALENDAR_DISPLAY_HOUR_TO, DEFAULT_CALENDAR_HOUR_TO)
    if h_from < 0:
        h_from = 0
    if h_to > 24:
        h_to = 24
    if h_from >= h_to:
        return DEFAULT_CALENDAR_HOUR_FROM, DEFAULT_CALENDAR_HOUR_TO
    return h_from, h_to


def _read_hour_setting(db: Session, key: str, default: int) -> int:
    row = db.get(Setting, key)
    if not row or not str(row.value or "").strip():
        return default
    try:
        return int(str(row.value).strip())
    except ValueError:
        return default
