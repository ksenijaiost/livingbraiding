"""Сетка занятости мастеров по броням на день."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import json
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
    WorkPlan,
    WorkPlanStatus,
)
from app.display_time import get_display_timezone
from app.planned_service_time import planned_start_local_datetime
from app.master_schedule import master_unavailable_for_day
from app.master_time_blocks import COLOR_TIME_BLOCK, list_time_blocks_for_masters_on_day
from app.ui_service_display import booking_service_labels_from_booking, format_service_catalog_path
from app.user_roles import select_users_with_role

COLOR_OCCUPANCY_ACTIVE = "#69d186"
COLOR_OCCUPANCY_PENDING = "#9CFF19"
COLOR_OCCUPANCY_CONSULTATION = "#f7d368"
COLOR_OCCUPANCY_WORK_PLAN = "#D8BFD8"
COLOR_OCCUPANCY_DAY_OFF = "#fc8580"
COLOR_OCCUPANCY_UNAVAILABLE = "#cfcfcf"
COLOR_OCCUPANCY_NO_DATA = "#ffffff"
CONSULTATION_DEFAULT_DURATION_MINUTES = 60


def occupancy_color_for_status(status: BookingStatus | str) -> str:
    """Цвет брони-визита по статусу (без учёта kind)."""
    return occupancy_color_for_booking(status, kind=BookingKind.VISIT)


def occupancy_color_for_booking(
    status: BookingStatus | str,
    *,
    kind: BookingKind | str | None = None,
) -> str:
    """Цвет сегмента занятости: консультации всегда жёлтые; визиты — по статусу."""
    k = kind.value if isinstance(kind, BookingKind) else (str(kind or "").strip().upper())
    s = status.value if isinstance(status, BookingStatus) else str(status)
    # Консультация: всегда жёлтая. DONE — тот же цвет + opacity в UI.
    if k == BookingKind.CONSULTATION.value:
        return COLOR_OCCUPANCY_CONSULTATION
    if s == BookingStatus.PENDING_CONFIRMATION.value:
        return COLOR_OCCUPANCY_PENDING
    return COLOR_OCCUPANCY_ACTIVE


@dataclass(frozen=True)
class OccupancySegment:
    master_id: int
    start_minutes: int
    end_minutes: int
    status: str
    color: str
    url: str
    client_name: str
    service_label: str
    booking_id: int | None = None
    work_plan_id: int | None = None
    kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "master_id": self.master_id,
            "start_minutes": self.start_minutes,
            "end_minutes": self.end_minutes,
            "status": self.status,
            "color": self.color,
            "url": self.url,
            "client_name": self.client_name,
            "service_label": self.service_label,
        }
        if self.booking_id is not None:
            out["booking_id"] = self.booking_id
        if self.work_plan_id is not None:
            out["work_plan_id"] = self.work_plan_id
        if self.kind:
            out["kind"] = self.kind
        return out


@dataclass(frozen=True)
class BlockOccupancySegment:
    block_id: int
    master_id: int
    start_minutes: int
    end_minutes: int
    comment: str
    color: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "block",
            "block_id": self.block_id,
            "master_id": self.master_id,
            "start_minutes": self.start_minutes,
            "end_minutes": self.end_minutes,
            "comment": self.comment,
            "color": self.color,
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


def _booking_client_name(booking: Booking) -> str:
    client = getattr(booking, "client", None)
    if client is None:
        return "—"
    name = (client.name or "").strip() or "—"
    if not getattr(client, "is_confirmed", True):
        return f"{name} (черновик)"
    return name


def _segment_service_label(booking: Booking, svc: Service | None) -> str:
    if svc is not None:
        return format_service_catalog_path(svc, prefer_short=True)
    return booking_service_labels_from_booking(booking, prefer_short=True)


def _booking_load_options():
    return (
        selectinload(Booking.client),
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
    exclude_work_plan_id: int | None = None,
) -> dict[str, Any]:
    tz_name = get_display_timezone(db)
    tz = ZoneInfo(tz_name)
    # Локальный календарный день → окно naive-UTC (как хранятся planned_date).
    day_start_utc = (
        datetime.combine(day, time.min, tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    )
    day_end_utc = (
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        .astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
    )

    if bookings is None:
        bookings = list(
            db.scalars(
                select(Booking)
                .where(
                    Booking.planned_date >= day_start_utc,
                    Booking.planned_date < day_end_utc,
                    Booking.status != BookingStatus.CANCELLED,
                    Booking.kind.in_((BookingKind.VISIT, BookingKind.CONSULTATION)),
                )
                .options(*_booking_load_options())
                .order_by(Booking.planned_date.asc(), Booking.id.asc())
            ).all()
        )

    grid_start = hour_from * 60
    grid_end = hour_to * 60
    segments: list[OccupancySegment] = []

    for b in bookings:
        if b.kind not in (BookingKind.VISIT, BookingKind.CONSULTATION) or b.status == BookingStatus.CANCELLED:
            continue
        status = b.status
        color = occupancy_color_for_booking(status, kind=b.kind)
        status_s = status.value if status else BookingStatus.ACTIVE.value
        kind_s = b.kind.value if b.kind else BookingKind.VISIT.value
        url = f"/bookings/{int(b.id)}"
        booking_master_ids = [int(m.master_id) for m in (b.masters or []) if m.master_id]
        client_name = _booking_client_name(b)

        # Длительность может быть переопределена в details_json.
        # Для VISIT: visit_custom_duration_minutes
        # Для CONSULTATION: consultation_duration_minutes (по умолчанию 60 мин).
        dur_override: int = 0
        raw_details = getattr(b, "details_json", None)
        if raw_details:
            try:
                d0 = json.loads(raw_details)
                if isinstance(d0, dict):
                    if b.kind == BookingKind.CONSULTATION:
                        dur_override = int(
                            d0.get("consultation_duration_minutes") or CONSULTATION_DEFAULT_DURATION_MINUTES
                        )
                    else:
                        dur_override = int(d0.get("visit_custom_duration_minutes") or 0)
            except Exception:
                dur_override = 0
        if dur_override < 0:
            dur_override = 0
        if b.kind == BookingKind.CONSULTATION:
            dur = dur_override or CONSULTATION_DEFAULT_DURATION_MINUTES
            start_local = _utc_naive_to_local(b.planned_date, tz_name)
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
                        client_name=client_name,
                        service_label="Консультация",
                        kind=kind_s,
                    )
                )
            continue

        lines = list(b.planned_services or [])
        if lines:
            # Для multi-service занятости по строкам planned_services оставляем
            # длительности услуг (override относится к "времени визита" в простой форме).
            use_override_for_single_line = dur_override > 0 and len(lines) == 1
            for ps in sorted(lines, key=lambda x: (int(x.sort_order or 0), int(x.id or 0))):
                svc = ps.service
                if svc is None:
                    continue
                line_dur = int(getattr(ps, "duration_minutes", None) or 0)
                if line_dur > 0:
                    dur = line_dur
                else:
                    dur = int(svc.estimated_duration_minutes or 0)
                    if use_override_for_single_line:
                        dur = int(dur_override)
                if dur <= 0:
                    continue
                if ps.planned_start_time:
                    start_local = planned_start_local_datetime(
                        ps.planned_start_time,
                        booking_planned_date=b.planned_date,
                        tz_name=tz_name,
                    ) or _utc_naive_to_local(b.planned_date, tz_name)
                else:
                    start_local = _utc_naive_to_local(b.planned_date, tz_name)
                start_m = _minutes_on_day(start_local, day)
                end_m = start_m + dur
                masters_rows = list(ps.masters or [])
                master_ids = [int(m.master_id) for m in masters_rows if m.master_id]
                if not master_ids:
                    master_ids = booking_master_ids
                per_master_start: dict[int, int] = {}
                for m in masters_rows:
                    if not m.master_id or not m.planned_start_time:
                        continue
                    m_local = planned_start_local_datetime(
                        m.planned_start_time,
                        booking_planned_date=b.planned_date,
                        tz_name=tz_name,
                    )
                    if m_local is None:
                        continue
                    per_master_start[int(m.master_id)] = _minutes_on_day(m_local, day)
                for mid in master_ids:
                    start_m_mid = per_master_start.get(mid, start_m)
                    if end_m <= grid_start or start_m_mid >= grid_end:
                        continue
                    segments.append(
                        OccupancySegment(
                            booking_id=int(b.id),
                            master_id=mid,
                            start_minutes=max(start_m_mid, grid_start),
                            end_minutes=min(end_m, grid_end),
                            status=status_s,
                            color=color,
                            url=url,
                            client_name=client_name,
                            service_label=_segment_service_label(b, svc),
                            kind=kind_s,
                        )
                    )
            continue

        svc = b.planned_service
        if svc is None:
            continue
        dur = dur_override if dur_override > 0 else int(svc.estimated_duration_minutes or 0)
        if dur <= 0:
            continue
        start_local = _utc_naive_to_local(b.planned_date, tz_name)
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
                    client_name=client_name,
                    service_label=_segment_service_label(b, svc),
                    kind=kind_s,
                )
            )

    from app.work_plan import planned_end_utc, work_plan_type_display

    wp_stmt = (
        select(WorkPlan)
        .where(
            WorkPlan.status == WorkPlanStatus.PLANNED,
            WorkPlan.planned_date >= day_start_utc,
            WorkPlan.planned_date < day_end_utc,
        )
        .order_by(WorkPlan.planned_date.asc(), WorkPlan.id.asc())
    )
    if exclude_work_plan_id:
        wp_stmt = wp_stmt.where(WorkPlan.id != exclude_work_plan_id)
    for wp in db.scalars(wp_stmt).all():
        start_local = _utc_naive_to_local(wp.planned_date, tz_name)
        start_m = _minutes_on_day(start_local, day)
        end_local = _utc_naive_to_local(planned_end_utc(wp), tz_name)
        end_m = _minutes_on_day(end_local, day)
        if end_m <= grid_start or start_m >= grid_end:
            continue
        segments.append(
            OccupancySegment(
                work_plan_id=int(wp.id),
                master_id=int(wp.master_id),
                start_minutes=max(start_m, grid_start),
                end_minutes=min(end_m, grid_end),
                status=WorkPlanStatus.PLANNED.value,
                color=COLOR_OCCUPANCY_WORK_PLAN,
                url=f"/work-plans/{int(wp.id)}",
                client_name="",
                service_label=work_plan_type_display(wp),
            )
        )

    masters = list_calendar_masters(db)
    master_ids = [int(m["id"]) for m in masters]
    block_segments: list[BlockOccupancySegment] = []
    for block in list_time_blocks_for_masters_on_day(db, master_ids=master_ids, block_date=day):
        bs = int(block.time_from.hour) * 60 + int(block.time_from.minute)
        be = int(block.time_to.hour) * 60 + int(block.time_to.minute)
        if be <= grid_start or bs >= grid_end:
            continue
        block_segments.append(
            BlockOccupancySegment(
                block_id=int(block.id),
                master_id=int(block.master_id),
                start_minutes=max(bs, grid_start),
                end_minutes=min(be, grid_end),
                comment=(block.comment or "").strip(),
                color=COLOR_TIME_BLOCK,
            )
        )

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
        "block_segments": [s.to_dict() for s in block_segments],
        "schedule": schedule,
        "colors": {
            "confirmed": COLOR_OCCUPANCY_ACTIVE,
            "pending": COLOR_OCCUPANCY_PENDING,
            "consultation": COLOR_OCCUPANCY_CONSULTATION,
            "day_off": COLOR_OCCUPANCY_DAY_OFF,
            "unavailable": COLOR_OCCUPANCY_UNAVAILABLE,
            "no_data": COLOR_OCCUPANCY_NO_DATA,
            "block": COLOR_TIME_BLOCK,
            "work_plan": COLOR_OCCUPANCY_WORK_PLAN,
        },
    }


def _merge_minute_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cleaned = [(int(s), int(e)) for s, e in intervals if int(e) > int(s)]
    if not cleaned:
        return []
    cleaned.sort(key=lambda x: x[0])
    out = [cleaned[0]]
    for s, e in cleaned[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


def _subtract_minute_intervals(span: tuple[int, int], blocks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    s0, e0 = int(span[0]), int(span[1])
    if e0 <= s0:
        return []
    result = [(s0, e0)]
    for bs, be in _merge_minute_intervals(blocks):
        new_result: list[tuple[int, int]] = []
        for rs, re in result:
            if be <= rs or bs >= re:
                new_result.append((rs, re))
            else:
                if rs < bs:
                    new_result.append((rs, bs))
                if be < re:
                    new_result.append((be, re))
        result = new_result
    return [(s, e) for s, e in result if e > s]


def occupied_intervals_for_master(occupancy: dict[str, Any], master_id: int) -> list[tuple[int, int]]:
    mid = int(master_id)
    occupied: list[tuple[int, int]] = []
    for seg in occupancy.get("segments") or []:
        if int(seg.get("master_id") or 0) == mid:
            occupied.append((int(seg["start_minutes"]), int(seg["end_minutes"])))
    for seg in occupancy.get("block_segments") or []:
        if int(seg.get("master_id") or 0) == mid:
            occupied.append((int(seg["start_minutes"]), int(seg["end_minutes"])))
    return occupied


def master_shift_has_free_time(
    *,
    work_start_m: int,
    work_end_m: int,
    break_ranges: list[tuple[int, int]] | None = None,
    occupied: list[tuple[int, int]] | None = None,
) -> bool:
    """Есть ли свободные минуты внутри рабочей смены (минус перерыв и занятость)."""
    spans = _subtract_minute_intervals(
        (int(work_start_m), int(work_end_m)),
        list(break_ranges or []),
    )
    occ = list(occupied or [])
    for ws, we in spans:
        free = _subtract_minute_intervals((ws, we), occ)
        if any(e > s for s, e in free):
            return True
    return False


def master_has_free_time_on_day(*, master_id: int, occupancy: dict[str, Any]) -> bool:
    """Есть ли у мастера свободное время в рабочий день (не перерыв, не бронь/блок/план)."""
    schedule = occupancy.get("schedule") or {}
    mid = int(master_id)
    sched = schedule.get(str(mid))
    if sched is None:
        sched = schedule.get(mid)
    if not isinstance(sched, dict) or sched.get("column_state") != "working":
        return False

    hour_from = int(occupancy.get("hour_from") or 0)
    hour_to = int(occupancy.get("hour_to") or 24)
    grid_start = hour_from * 60
    grid_end = hour_to * 60

    unavailable = []
    for u in sched.get("unavailable") or []:
        if not isinstance(u, dict):
            continue
        try:
            unavailable.append((int(u["start_minutes"]), int(u["end_minutes"])))
        except (KeyError, TypeError, ValueError):
            continue

    # Свободное время считаем по пересечению сетки с сменой (grid − unavailable),
    # а не только по «дырам» внутри сетки без привязки к смене.
    working_spans = _subtract_minute_intervals((grid_start, grid_end), unavailable)
    occupied = occupied_intervals_for_master(occupancy, mid)
    for ws, we in working_spans:
        if master_shift_has_free_time(
            work_start_m=ws,
            work_end_m=we,
            break_ranges=[],
            occupied=occupied,
        ):
            return True
    return False
