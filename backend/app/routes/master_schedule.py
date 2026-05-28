from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import MasterScheduleDay, MasterScheduleStatus, User, UserRole
from app.db.session import get_db
from app.user_roles import select_users_with_role
from app.forms_parse import parse_date_iso
from app.master_schedule import (
    apply_cyclic_bulk,
    apply_weekday_bulk,
    day_state,
    get_default_work_hours,
    schedule_filled_until,
    save_master_schedule_day,
)
from app.webui import templates, ctx as _ctx


def _month_range_utc(db: Session, ym: str) -> tuple[datetime, datetime]:
    """UTC-naive range for the given display-timezone month."""
    from zoneinfo import ZoneInfo

    from app.display_time import get_display_timezone

    year, month = _parse_ym(ym)
    tz = ZoneInfo(get_display_timezone(db))
    month_local_start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        next_month_local_start = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        next_month_local_start = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)
    start_utc = month_local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = next_month_local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return start_utc, end_utc


router = APIRouter()


def _parse_ym(m: str) -> tuple[int, int]:
    m = (m or "").strip()
    if len(m) != 7 or m[4] != "-":
        raise ValueError("bad ym")
    y = int(m[:4])
    mo = int(m[5:])
    if not (1 <= mo <= 12):
        raise ValueError("bad ym")
    return y, mo


def _parse_hhmm(v: str | None) -> time | None:
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    # ожидаем "HH:MM"
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))


def _day_row_to_payload(row: MasterScheduleDay | None, *, hour_from: int, hour_to: int) -> dict[str, Any]:
    if row is None:
        return {"state": "no_data"}
    payload: dict[str, Any] = {"state": "day_off" if row.status == MasterScheduleStatus.DAY_OFF else "working"}
    if row.status == MasterScheduleStatus.WORKING:
        payload["time_from"] = (row.time_from.isoformat(timespec="minutes") if row.time_from else None)
        payload["time_to"] = (row.time_to.isoformat(timespec="minutes") if row.time_to else None)
        payload["break_from"] = (row.break_from.isoformat(timespec="minutes") if row.break_from else None)
        payload["break_to"] = (row.break_to.isoformat(timespec="minutes") if row.break_to else None)
        # Для UI удобно сразу показать реальные минуты, с учётом дефолтов.
        default_start = hour_from * 60
        default_end = hour_to * 60
        start_min = (row.time_from.hour * 60 + row.time_from.minute) if row.time_from else default_start
        end_min = (row.time_to.hour * 60 + row.time_to.minute) if row.time_to else default_end
        if end_min <= start_min:
            start_min, end_min = default_start, default_end
        payload["resolved_start_minutes"] = start_min
        payload["resolved_end_minutes"] = end_min
    return payload


@router.get("/api/master-schedule/month")
def api_master_schedule_month(
    m: str,
    user_id: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    year, month = _parse_ym(m)
    master_id = current_user.id if current_user.role == UserRole.MASTER else int(user_id or 0)
    if master_id <= 0:
        raise HTTPException(status_code=400, detail="user_id required")

    hour_from, hour_to = get_default_work_hours(db)
    filled = schedule_filled_until(db, master_id=master_id)

    first = date(year, month, 1)
    next_m = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    total_days = (next_m - first).days
    days: list[dict[str, Any]] = []
    for i in range(total_days):
        d0 = first.fromordinal(first.toordinal() + i)
        st = day_state(db, master_id=master_id, d=d0)
        days.append({"date": d0.isoformat(), "state": st})

    return JSONResponse(
        {
            "month": m,
            "filled_until": filled.isoformat() if filled else None,
            "hour_from": hour_from,
            "hour_to": hour_to,
            "days": days,
        }
    )


@router.get("/api/schedule/month")
def api_schedule_month_stats(
    m: str,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    """Статистика для админ-календаря в брони: working мастера + брони на день."""
    from sqlalchemy import func

    from app.db.models import Booking, BookingKind, BookingStatus
    from app.display_time import get_display_timezone
    from zoneinfo import ZoneInfo

    year, month = _parse_ym(m)
    start_utc, end_utc = _month_range_utc(db, m)

    # День → количество броней (VISIT, pending/active)
    tz = ZoneInfo(get_display_timezone(db))

    booking_rows = list(
        db.execute(
            select(Booking.planned_date)
            .where(
                Booking.kind == BookingKind.VISIT,
                Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)),
                Booking.planned_date >= start_utc,
                Booking.planned_date < end_utc,
            )
        ).all()
    )
    bookings_by_day: dict[date, int] = {}
    for (dt0,) in booking_rows:
        if not isinstance(dt0, datetime):
            continue
        d0 = dt0.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
        bookings_by_day[d0] = int(bookings_by_day.get(d0, 0) + 1)

    # День → количество работающих мастеров (есть строка WORKING)
    # Неактивных мастеров исключаем.
    from app.db.models import MasterScheduleDay, MasterScheduleStatus, User

    ms_rows = list(
        db.execute(
            select(MasterScheduleDay.work_date, func.count(func.distinct(MasterScheduleDay.master_id)))
            .join(User, User.id == MasterScheduleDay.master_id)
            .where(
                User.is_active.is_(True),
                MasterScheduleDay.status == MasterScheduleStatus.WORKING,
                MasterScheduleDay.work_date >= date(year, month, 1),
                MasterScheduleDay.work_date < (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)),
            )
            .group_by(MasterScheduleDay.work_date)
        ).all()
    )
    working_by_day: dict[date, int] = {d0: int(cnt or 0) for d0, cnt in ms_rows if isinstance(d0, date)}

    first = date(year, month, 1)
    next_m = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    total_days = (next_m - first).days
    days: list[dict[str, Any]] = []
    for i in range(total_days):
        d0 = first.fromordinal(first.toordinal() + i)
        days.append(
            {
                "date": d0.isoformat(),
                "working_masters": int(working_by_day.get(d0, 0)),
                "bookings": int(bookings_by_day.get(d0, 0)),
            }
        )

    return JSONResponse({"month": m, "days": days})


@router.get("/api/master-schedule/day")
def api_admin_master_schedule_day(
    d: str,
    user_id: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    day = parse_date_iso(d, field_name="d")
    master_id = current_user.id if current_user.role == UserRole.MASTER else int(user_id or 0)
    if master_id <= 0:
        raise HTTPException(status_code=400, detail="user_id required")
    hour_from, hour_to = get_default_work_hours(db)
    row = db.scalar(
        select(MasterScheduleDay).where(
            MasterScheduleDay.master_id == master_id, MasterScheduleDay.work_date == day
        )
    )
    payload = _day_row_to_payload(row, hour_from=hour_from, hour_to=hour_to)
    payload["date"] = day.isoformat()
    return JSONResponse(payload)


@router.post("/api/master-schedule/day")
async def api_save_master_schedule_day(
    request: Request,
    d: str = Form(...),
    user_id: int | None = Form(None),
    status: str = Form(...),
    time_from: str | None = Form(None),
    time_to: str | None = Form(None),
    break_enabled: str | None = Form(None),
    break_from: str | None = Form(None),
    break_to: str | None = Form(None),
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    day = parse_date_iso(d, field_name="d")
    master_id = current_user.id if current_user.role == UserRole.MASTER else int(user_id or 0)
    if master_id <= 0:
        raise HTTPException(status_code=400, detail="user_id required")

    try:
        st = MasterScheduleStatus(status)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad status")

    tf = _parse_hhmm(time_from)
    tt = _parse_hhmm(time_to)

    br_enabled = (break_enabled or "").strip().lower() in ("1", "true", "yes", "on")
    bf = _parse_hhmm(break_from) if br_enabled else None
    bt = _parse_hhmm(break_to) if br_enabled else None

    save_master_schedule_day(
        db,
        master_id=master_id,
        work_date=day,
        status=st,
        time_from=tf,
        time_to=tt,
        break_from=bf,
        break_to=bt,
        changed_by_user_id=current_user.id,
    )
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/api/master-schedule/bulk")
async def api_save_master_schedule_bulk(
    request: Request,
    date_from: str = Form(...),
    date_to: str = Form(...),
    master_id: int | None = Form(None),
    mode: str = Form("WEEKDAY"),
    # WEEKDAY
    weekday_work_0: str | None = Form(None),
    weekday_work_1: str | None = Form(None),
    weekday_work_2: str | None = Form(None),
    weekday_work_3: str | None = Form(None),
    weekday_work_4: str | None = Form(None),
    weekday_work_5: str | None = Form(None),
    weekday_work_6: str | None = Form(None),
    # CUSTOM CYCLIC
    scheme: str = Form("CUSTOM"),
    odd_even_mode: str = Form("ODD"),
    custom_work_days: int | None = Form(None),
    custom_day_off_days: int | None = Form(None),
    # time interval
    time_from: str | None = Form(None),
    time_to: str | None = Form(None),
    break_enabled: str | None = Form(None),
    break_from: str | None = Form(None),
    break_to: str | None = Form(None),
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    d_from = parse_date_iso(date_from, field_name="date_from")
    d_to = parse_date_iso(date_to, field_name="date_to")
    real_master_id = current_user.id if current_user.role == UserRole.MASTER else int(master_id or 0)
    if real_master_id <= 0:
        raise HTTPException(status_code=400, detail="master_id required")

    tf = _parse_hhmm(time_from)
    tt = _parse_hhmm(time_to)
    br_enabled = (break_enabled or "").strip().lower() in ("1", "true", "yes", "on")
    bf = _parse_hhmm(break_from) if br_enabled else None
    bt = _parse_hhmm(break_to) if br_enabled else None

    if (mode or "").strip().upper() == "WEEKDAY":
        wk: set[int] = set()
        for i, v in enumerate(
            [
                weekday_work_0,
                weekday_work_1,
                weekday_work_2,
                weekday_work_3,
                weekday_work_4,
                weekday_work_5,
                weekday_work_6,
            ]
        ):
            if (v or "").strip().lower() in ("1", "true", "yes", "on"):
                wk.add(i)
        apply_weekday_bulk(
            db,
            master_id=real_master_id,
            date_from=d_from,
            date_to=d_to,
            working_weekdays=wk,
            time_from=tf,
            time_to=tt,
            break_from=bf,
            break_to=bt,
            changed_by_user_id=current_user.id,
        )
    else:
        # CYCLIC
        scheme_map = {
            "ALL_DAYS": "ALL_DAYS",
            "WEEKDAYS": "WEEKDAYS",
            "ODD_EVEN": "ODD_EVEN",
            "CUSTOM": "CUSTOM",
        }
        sch = scheme_map.get((scheme or "").strip().upper(), "CUSTOM")
        oem = (odd_even_mode or "ODD").strip().upper()
        if oem not in ("ODD", "EVEN"):
            oem = "ODD"
        apply_cyclic_bulk(
            db,
            master_id=real_master_id,
            date_from=d_from,
            date_to=d_to,
            scheme=sch,  # type: ignore[arg-type]
            odd_even_mode=oem,  # type: ignore[arg-type]
            custom_work_days=int(custom_work_days or 1),
            custom_day_off_days=int(custom_day_off_days or 1),
            time_from=tf,
            time_to=tt,
            break_from=bf,
            break_to=bt,
            changed_by_user_id=current_user.id,
        )

    db.commit()
    return JSONResponse({"ok": True})


# ----------------------
# Pages (UI)
# ----------------------


@router.get("/master/schedule", response_class=HTMLResponse)
def master_schedule_page(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    if current_user.role == UserRole.ADMIN_SUPER:
        return RedirectResponse(url="/admin/master-schedule", status_code=303)
    # Template itself does all loading via API, we only provide initial master list.
    # Provide hour defaults for quick placeholders.
    hour_from, hour_to = get_default_work_hours(db)
    return templates.TemplateResponse(
        "master_schedule.html",
        _ctx(
            request,
            current_user=current_user,
            is_admin_super=False,
            master_id=int(current_user.id),
            masters=[],
            hour_from=hour_from,
            hour_to=hour_to,
            today_iso=date.today().isoformat(),
        ),
    )


@router.get("/admin/master-schedule", response_class=HTMLResponse)
def admin_master_schedule_page(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    masters = list(db.scalars(select_users_with_role(UserRole.MASTER).order_by(User.display_name.asc())).all())
    initial_master_id = int(masters[0].id) if masters else int(current_user.id)
    hour_from, hour_to = get_default_work_hours(db)
    return templates.TemplateResponse(
        "master_schedule.html",
        _ctx(
            request,
            current_user=current_user,
            is_admin_super=True,
            master_id=initial_master_id,
            masters=[{"id": int(m.id), "name": m.display_name or m.username} for m in masters],
            hour_from=hour_from,
            hour_to=hour_to,
            today_iso=date.today().isoformat(),
        ),
    )

