"""Сетка занятости мастеров по броням на день."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Booking,
    BookingKind,
    BookingPlannedService,
    BookingStatus,
    Service,
    ServiceSubcategory,
    User,
    UserRole,
)
from app.display_time import get_display_timezone
from app.master_schedule import master_unavailable_for_day
from app.user_roles import select_users_with_role

COLOR_OCCUPANCY_ACTIVE = "#9E88C0"
COLOR_OCCUPANCY_PENDING = "#7F7679"


def occupancy_color_for_status(status: BookingStatus | str) -> str:
    s = status.value if isinstance(status, BookingStatus) else str(status)
    if s == BookingStatus.PENDING_CONFIRMATION.value:
        return COLOR_OCCUPANCY_PENDING
    return COLOR_OCCUPANCY_ACTIVE


@dataclass(frozen=True)
class OccupancySegment:
    booking_id: int
    master_id: int
    start_minutes: int
    end_minutes: int
    status: str
    color: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "booking_id": self.booking_id,
            "master_id": self.master_id,
            "start_minutes": self.start_minutes,
            "end_minutes": self.end_minutes,
            "status": self.status,
            "color": self.color,
            "url": self.url,
        }


def _utc_naive_to_local(dt: datetime, tz_name: str) -> datetime:
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def _minutes_on_day(local_dt: datetime, day: date) -> int:
    base = datetime.combine(day, datetime.min.time())
    delta = local_dt - base
    return max(0, int(delta.total_seconds() // 60))


def list_calendar_masters(db: Session) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select_users_with_role(UserRole.MASTER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )
    return [{"id": int(u.id), "name": u.display_name or u.username} for u in rows]


def _booking_load_options():
    return (
        selectinload(Booking.masters),
        selectinload(Booking.planned_service)
        .selectinload(Service.subcategory)
        .selectinload(ServiceSubcategory.category),
        selectinload(Booking.planned_services)
        .selectinload(BookingPlannedService.service)
        .selectinload(Service.subcategory)
        .selectinload(ServiceSubcategory.category),
        selectinload(Booking.planned_services).selectinload(BookingPlannedService.masters),
    )


def build_occupancy_for_day(
    db: Session,
    *,
    day: date,
    hour_from: int,
    hour_to: int,
    bookings: list[Booking] | None = None,
) -> dict[str, Any]:
    tz = get_display_timezone(db)
    day_start_utc = datetime.combine(day, datetime.min.time())
    day_end_utc = day_start_utc + timedelta(days=1)

    if bookings is None:
        bookings = list(
            db.scalars(
                select(Booking)
                .where(
                    Booking.planned_date >= day_start_utc,
                    Booking.planned_date < day_end_utc,
                    Booking.status != BookingStatus.CANCELLED,
                    Booking.kind == BookingKind.VISIT,
                )
                .options(*_booking_load_options())
                .order_by(Booking.planned_date.asc(), Booking.id.asc())
            ).all()
        )

    grid_start = hour_from * 60
    grid_end = hour_to * 60
    segments: list[OccupancySegment] = []

    for b in bookings:
        if b.kind != BookingKind.VISIT or b.status == BookingStatus.CANCELLED:
            continue
        status = b.status
        color = occupancy_color_for_status(status)
        status_s = status.value if status else BookingStatus.ACTIVE.value
        url = f"/bookings/{int(b.id)}"
        booking_master_ids = [int(m.master_id) for m in (b.masters or []) if m.master_id]

        lines = list(b.planned_services or [])
        if lines:
            for ps in sorted(lines, key=lambda x: (int(x.sort_order or 0), int(x.id or 0))):
                svc = ps.service
                if svc is None:
                    continue
                dur = int(svc.estimated_duration_minutes or 0)
                if dur <= 0:
                    continue
                if ps.planned_start_time:
                    start_local = _utc_naive_to_local(ps.planned_start_time, tz)
                else:
                    start_local = _utc_naive_to_local(b.planned_date, tz)
                start_m = _minutes_on_day(start_local, day)
                end_m = start_m + dur
                master_ids = [int(m.master_id) for m in (ps.masters or []) if m.master_id]
                if not master_ids:
                    master_ids = booking_master_ids
                for mid in master_ids:
                    if end_m <= grid_start or start_m >= grid_end:
                        continue
                    segments.append(
                        OccupancySegment(
                            booking_id=int(b.id),
                            master_id=mid,
                            start_minutes=max(start_m, grid_start),
                            end_minutes=min(end_m, grid_end),
                            status=status_s,
                            color=color,
                            url=url,
                        )
                    )
            continue

        svc = b.planned_service
        if svc is None:
            continue
        dur = int(svc.estimated_duration_minutes or 0)
        if dur <= 0:
            continue
        start_local = _utc_naive_to_local(b.planned_date, tz)
        start_m = _minutes_on_day(start_local, day)
        end_m = start_m + dur
        for mid in booking_master_ids:
            if end_m <= grid_start or start_m >= grid_end:
                continue
            segments.append(
                OccupancySegment(
                    booking_id=int(b.id),
                    master_id=mid,
                    start_minutes=max(start_m, grid_start),
                    end_minutes=min(end_m, grid_end),
                    status=status_s,
                    color=color,
                    url=url,
                )
            )

    masters = list_calendar_masters(db)
    schedule: dict[str, Any] = {}
    for m in masters:
        mid = int(m["id"])
        st, unavailable = master_unavailable_for_day(
            db,
            master_id=mid,
            d=day,
            hour_from=hour_from,
            hour_to=hour_to,
        )
        schedule[str(mid)] = {
            "column_state": st,
            "unavailable": [
                {"start_minutes": u.start_minutes, "end_minutes": u.end_minutes}
                for u in unavailable
            ],
        }

    return {
        "hour_from": hour_from,
        "hour_to": hour_to,
        "masters": masters,
        "segments": [s.to_dict() for s in segments],
        "schedule": schedule,
    }
