from __future__ import annotations

from datetime import date, datetime, time


def payroll_period_day_start(d: date) -> datetime:
    return datetime.combine(d, time.min)


def payroll_period_day_end(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59, 999999))

