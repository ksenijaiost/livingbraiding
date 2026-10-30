from __future__ import annotations

from datetime import datetime
from typing import overload
from zoneinfo import ZoneInfo


def utcnow_naive() -> datetime:
    """
    Return naive UTC datetime.

    We keep DB fields naive-UTC for now (no timezone-aware migration yet).
    """

    return datetime.utcnow()


@overload
def utc_naive_to_local(dt: datetime, tz_name: str) -> datetime: ...


@overload
def utc_naive_to_local(dt: None, tz_name: str) -> None: ...


def utc_naive_to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    """Naive-UTC (как в БД) → naive-локальное время в часовом поясе `tz_name`.

    `None` возвращается как `None`. Если пришёл aware-datetime, он сначала приводится к UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def local_naive_to_utc_naive(dt: datetime, tz_name: str) -> datetime:
    """Naive-локальное время в поясе `tz_name` → naive-UTC (для записи в БД)."""
    local_dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
