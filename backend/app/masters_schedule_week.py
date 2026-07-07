"""Недельная сводка графика всех мастеров для страницы «График всех мастеров»."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.calendar_occupancy import COLOR_OCCUPANCY_DAY_OFF, COLOR_OCCUPANCY_NO_DATA
from app.db.models import (
    Booking,
    BookingKind,
    BookingPlannedService,
    BookingStatus,
    MasterScheduleDay,
    MasterScheduleStatus,
    User,
    UserRole,
)
from app.display_time import get_display_timezone
from app.master_schedule import day_state
from app.user_roles import select_users_with_role

COLOR_SCHEDULE_WORKING = "#bae6fd"
COLOR_BOOKING_DOT = "#f97316"

_WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def master_initials(user: User) -> str:
    name = (user.display_name or user.username or "").strip()
    if not name:
        return "?"
    parts = [p for p in name.replace(".", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


def _schedule_row_payload(row: MasterScheduleDay | None) -> dict[str, Any]:
    if row is None or row.status == MasterScheduleStatus.DAY_OFF:
        return {"state": "day_off", "time_from": None, "time_to": None}
    tf = row.time_from.isoformat(timespec="minutes") if row.time_from else None
    tt = row.time_to.isoformat(timespec="minutes") if row.time_to else None
    return {"state": "working", "time_from": tf, "time_to": tt}


def _masters_with_bookings(
    db: Session,
    *,
    week_start: date,
    week_end: date,
    tz: ZoneInfo,
) -> set[tuple[int, date]]:
    """Множество (master_id, local_date) с активными/ожидающими бронями VISIT."""
    start_utc = datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz).astimezone(
        ZoneInfo("UTC")
    ).replace(tzinfo=None)
    end_utc = (
        datetime(week_end.year, week_end.month, week_end.day, tzinfo=tz)
        + timedelta(days=1)
    ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    rows = list(
        db.scalars(
            select(Booking)
            .where(
                Booking.kind == BookingKind.VISIT,
                Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)),
                Booking.planned_date >= start_utc,
                Booking.planned_date < end_utc,
            )
            .options(
                selectinload(Booking.masters),
                selectinload(Booking.planned_services).selectinload(BookingPlannedService.masters),
            )
        ).all()
    )
    out: set[tuple[int, date]] = set()
    for b in rows:
        local_d = b.planned_date.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
        master_ids: set[int] = set()
        for bm in b.masters or []:
            if bm.master_id:
                master_ids.add(int(bm.master_id))
        for ps in b.planned_services or []:
            for psm in ps.masters or []:
                if psm.master_id:
                    master_ids.add(int(psm.master_id))
        for mid in master_ids:
            out.add((mid, local_d))
    return out


def build_masters_schedule_week(db: Session, *, week_start: date) -> dict[str, Any]:
    week_start = monday_of_week(week_start)
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    week_end = week_days[-1]
    tz = ZoneInfo(get_display_timezone(db))
    today = datetime.now(tz).date()

    masters = list(
        db.scalars(
            select_users_with_role(UserRole.MASTER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )

    schedule_rows = list(
        db.scalars(
            select(MasterScheduleDay).where(
                MasterScheduleDay.work_date >= week_start,
                MasterScheduleDay.work_date <= week_end,
            )
        ).all()
    )
    schedule_by_key: dict[tuple[int, date], MasterScheduleDay] = {}
    for row in schedule_rows:
        schedule_by_key[(int(row.master_id), row.work_date)] = row

    booking_keys = _masters_with_bookings(
        db, week_start=week_start, week_end=week_end, tz=tz
    )

    days_out: list[dict[str, Any]] = []
    for i, d0 in enumerate(week_days):
        days_out.append(
            {
                "date": d0.isoformat(),
                "weekday_short": _WEEKDAY_SHORT[i],
                "day_num": d0.day,
                "is_today": d0 == today,
                "is_weekend": i >= 5,
            }
        )

    masters_out: list[dict[str, Any]] = []
    for m in masters:
        mid = int(m.id)
        cells: list[dict[str, Any]] = []
        for d0 in week_days:
            st = day_state(db, master_id=mid, d=d0)
            row = schedule_by_key.get((mid, d0))
            if st == "working":
                cell = _schedule_row_payload(row)
            elif st == "day_off":
                cell = {"state": "day_off", "time_from": None, "time_to": None}
            else:
                cell = {"state": "no_data", "time_from": None, "time_to": None}
            cell["has_booking"] = (mid, d0) in booking_keys
            cells.append(cell)
        masters_out.append(
            {
                "id": mid,
                "name": (m.display_name or m.username or "").strip() or f"#{mid}",
                "initials": master_initials(m),
                "cells": cells,
            }
        )

    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "today": today.isoformat(),
        "colors": {
            "no_data": COLOR_OCCUPANCY_NO_DATA,
            "day_off": COLOR_OCCUPANCY_DAY_OFF,
            "working": COLOR_SCHEDULE_WORKING,
            "booking_dot": COLOR_BOOKING_DOT,
        },
        "days": days_out,
        "masters": masters_out,
    }
