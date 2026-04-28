"""Отображение дат/времени в часовом поясе из настроек (хранимые в БД значения — naive UTC)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.models import Setting
from app.setting_keys import DISPLAY_TIMEZONE

# На сервере визиты/reserved_at пишутся через utcnow_naive() (naive = UTC).
DEFAULT_DISPLAY_TIMEZONE = "Asia/Novosibirsk"

ALLOWED_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Asia/Novosibirsk", "Новосибирск"),
    ("Asia/Krasnoyarsk", "Красноярск"),
    ("Asia/Omsk", "Омск"),
    ("Europe/Moscow", "Москва"),
    ("Asia/Yekaterinburg", "Екатеринбург"),
    ("UTC", "UTC"),
)

ALLOWED_TIMEZONE_IDS = frozenset(t[0] for t in ALLOWED_TIMEZONES)


def get_display_timezone(db: Session) -> str:
    row = db.get(Setting, DISPLAY_TIMEZONE)
    v = (row.value if row else "").strip()
    if v in ALLOWED_TIMEZONE_IDS:
        return v
    return DEFAULT_DISPLAY_TIMEZONE


def timezone_label(tz_id: str) -> str:
    for tid, lab in ALLOWED_TIMEZONES:
        if tid == tz_id:
            return lab
    return tz_id


def format_naive_utc_datetime(dt: datetime | None, tz_name: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Naive datetime считаем UTC, переводим в tz_name для показа."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).strftime(fmt)
