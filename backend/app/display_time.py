"""Отображение дат/времени в часовом поясе из настроек (хранимые в БД значения — naive UTC)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

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


def resolve_request_display_timezone(request: Request | None) -> str:
    if request is None:
        return DEFAULT_DISPLAY_TIMEZONE
    state = getattr(request, "state", None)
    tz = getattr(state, "display_timezone", None) if state is not None else None
    if isinstance(tz, str) and tz in ALLOWED_TIMEZONE_IDS:
        return tz
    return DEFAULT_DISPLAY_TIMEZONE


class DisplayTimezoneMiddleware(BaseHTTPMiddleware):
    """Кладёт display_timezone из настроек в request.state на каждый HTTP-запрос."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        from app.db.session import SessionLocal

        try:
            with SessionLocal() as db:
                request.state.display_timezone = get_display_timezone(db)
        except Exception:
            request.state.display_timezone = DEFAULT_DISPLAY_TIMEZONE
        return await call_next(request)
