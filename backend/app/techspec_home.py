"""Техническая сводка для главной страницы (роль TECHSPEC)."""

from __future__ import annotations

import sys
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session

from app.db.models import Booking, Client, User, Visit
from app.display_time import get_display_timezone
from app.media_store import media_backup_stats
from app.settings import get_settings


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} МБ"
    return f"{n / (1024 * 1024 * 1024):.2f} ГБ"


def _database_label() -> str:
    url = make_url(get_settings().database_url)
    if url.drivername.startswith("sqlite"):
        db_path = url.database or ":memory:"
        return f"SQLite · {db_path}"
    host = url.host or "localhost"
    port = f":{url.port}" if url.port else ""
    db_name = url.database or ""
    return f"{url.drivername} · {host}{port}/{db_name}"


def collect_techspec_home_stats(db: Session) -> dict[str, Any]:
    media = media_backup_stats()
    media["total_size_human"] = _fmt_bytes(int(media.get("total_bytes") or 0))

    users_active = int(db.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True))) or 0)
    clients_total = int(db.scalar(select(func.count()).select_from(Client)) or 0)
    visits_total = int(
        db.scalar(select(func.count()).select_from(Visit).where(Visit.is_cancelled.is_(False))) or 0
    )
    bookings_total = int(db.scalar(select(func.count()).select_from(Booking)) or 0)

    py = sys.version_info
    settings = get_settings()

    return {
        "media": media,
        "database": _database_label(),
        "app_env": settings.app_env,
        "display_tz": get_display_timezone(db),
        "python_version": f"{py.major}.{py.minor}.{py.micro}",
        "counts": {
            "users_active": users_active,
            "clients": clients_total,
            "visits": visits_total,
            "bookings": bookings_total,
        },
    }
