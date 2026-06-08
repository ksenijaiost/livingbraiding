from __future__ import annotations

from datetime import datetime, time

from app.planned_service_time import (
    coerce_planned_start_utc_naive,
    format_planned_service_start_local,
    planned_start_local_datetime,
)


def test_coerce_time_with_booking_date() -> None:
    booking_dt = datetime(2026, 6, 4, 3, 0)
    got = coerce_planned_start_utc_naive(time(3, 0), booking_planned_date=booking_dt)
    assert got == datetime(2026, 6, 4, 3, 0)


def test_coerce_datetime_passthrough() -> None:
    src = datetime(2026, 6, 4, 3, 0, 15)
    got = coerce_planned_start_utc_naive(src, booking_planned_date=datetime(2026, 6, 4, 10, 0))
    assert got == datetime(2026, 6, 4, 3, 0)


def test_format_time_value_for_display() -> None:
    booking_dt = datetime(2026, 6, 4, 3, 0)
    txt = format_planned_service_start_local(
        time(3, 0),
        booking_planned_date=booking_dt,
        tz_name="Asia/Novosibirsk",
        fmt="%H:%M",
    )
    assert txt == "10:00"


def test_planned_start_local_datetime_from_time() -> None:
    booking_dt = datetime(2026, 6, 4, 3, 0)
    local = planned_start_local_datetime(
        time(3, 0),
        booking_planned_date=booking_dt,
        tz_name="Asia/Novosibirsk",
    )
    assert local == datetime(2026, 6, 4, 10, 0)
