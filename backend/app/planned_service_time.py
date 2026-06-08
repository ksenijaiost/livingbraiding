"""Нормализация planned_start_time услуги в брони (Time или DateTime в БД)."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


def coerce_planned_start_utc_naive(
    value: datetime | time | None,
    *,
    booking_planned_date: datetime | None,
) -> datetime | None:
    """Привести planned_start_time к naive UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, time):
        ref = booking_planned_date or datetime.utcnow()
        return datetime.combine(ref.date(), value).replace(second=0, microsecond=0)
    return None


def planned_start_local_datetime(
    value: datetime | time | None,
    *,
    booking_planned_date: datetime | None,
    tz_name: str,
) -> datetime | None:
    utc_dt = coerce_planned_start_utc_naive(value, booking_planned_date=booking_planned_date)
    if utc_dt is None:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = utc_dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def format_planned_service_start_local(
    value: datetime | time | None,
    *,
    booking_planned_date: datetime | None,
    tz_name: str,
    fmt: str = "%H:%M",
) -> str:
    local_dt = planned_start_local_datetime(
        value,
        booking_planned_date=booking_planned_date,
        tz_name=tz_name,
    )
    if local_dt is None:
        return ""
    return local_dt.strftime(fmt)
