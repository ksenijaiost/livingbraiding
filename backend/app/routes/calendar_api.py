from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.consultation_booking import booking_status_label
from app.calendar_display import get_calendar_display_hours
from app.calendar_occupancy import build_occupancy_for_day, list_calendar_masters
from app.master_schedule import is_master_available_for_interval
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingPlannedService,
    BookingStaff,
    BookingStatus,
    Service,
    ServiceSubcategory,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    UserRole,
    Visit,
    VisitMaster,
    VisitMastersScope,
    VisitService,
    VisitServiceMaster,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
)
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime
from app.display_time import get_display_timezone
from app.forms_parse import parse_date_iso
from app.payroll_fund import sum_ledger_amounts_by_source
from app.ui_service_display import (
    booking_service_labels_from_booking,
    format_visit_service_catalog_path,
)


router = APIRouter()


def _money0(x: float | None) -> float:
    if x is None:
        return 0.0
    v = float(x)
    return 0.0 if abs(v) < 0.0005 else v


def _day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time.min)
    end_excl = start + timedelta(days=1)
    return start, end_excl


def _booking_kind_label(k: str) -> str:
    if k == BookingKind.VISIT.value:
        return "Визит (услуга)"
    if k == BookingKind.PRODUCT_SALE.value:
        return "Продажа (без услуги)"
    if k == BookingKind.CONSULTATION.value:
        return "Консультация"
    return k


def _work_activity_label(w: WorkForInventory) -> str:
    scope = "В наличие" if w.scope == WorkScope.IN_STOCK else "На заказ"
    kind_map = {
        WorkKind.KIT: "комплект/заготовки",
        WorkKind.MIX: "смешка",
        WorkKind.RUBBER: "хвосты/резинки",
        WorkKind.KIT_CORRECTION: "коррекция комплекта",
        WorkKind.OTHER: "другое",
        WorkKind.HAIR_EXT_PREP: "подготовка к наращиванию",
    }
    kind_l = kind_map.get(w.kind, w.kind.value)
    return f"Работа: {kind_l} ({scope})"


def _sum_ledger(
    db: Session,
    *,
    side: PayrollFundSide,
    source_kind: PayrollFundSourceKind,
    source_ids: list[int],
    user_id: int | None = None,
) -> dict[int, float]:
    raw = sum_ledger_amounts_by_source(
        db, side=side, source_kind=source_kind, source_ids=source_ids, user_id=user_id
    )
    return {k: _money0(v) for k, v in raw.items()}


@router.get("/api/calendar/day")
def api_calendar_day(
    d: str,
    view: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    try:
        day = parse_date_iso((d or "").strip(), field_name="d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная дата")

    day_start, day_end = _day_bounds_utc(day)

    is_master = current_user.role == UserRole.MASTER
    is_super = current_user.role == UserRole.ADMIN_SUPER

    # ---- Bookings ----
    b_stmt = (
        select(Booking)
        .options(
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
        .where(Booking.planned_date >= day_start, Booking.planned_date < day_end)
        .order_by(Booking.planned_date.asc(), Booking.id.asc())
    )
    if is_master:
        b_stmt = b_stmt.where(
            or_(
                Booking.id.in_(select(BookingMaster.booking_id).where(BookingMaster.master_id == current_user.id)),
                Booking.id.in_(select(BookingStaff.booking_id).where(BookingStaff.user_id == current_user.id)),
            )
        )
    bookings = list(db.scalars(b_stmt).all())

    booking_items: list[dict[str, Any]] = []
    tz = get_display_timezone(db)
    for b in bookings:
        kind_l = _booking_kind_label(b.kind.value)
        time_l = format_naive_utc_datetime(b.planned_date, tz)
        svc_label = ""
        if b.kind == BookingKind.VISIT:
            svc_label = booking_service_labels_from_booking(b)
        booking_items.append(
            {
                "id": int(b.id),
                "client": (b.client.name if b.client else "—"),
                "kind": kind_l,
                "label": f"{kind_l} · {time_l}",
                "service_label": svc_label,
                "status": booking_status_label(b.status),
                "time": time_l,
                "url": f"/bookings/{int(b.id)}",
                "payout_sum": 0.0,
                "studio_sum": 0.0,
            }
        )

    # ---- Visits ----
    v_stmt = (
        select(Visit)
        .options(selectinload(Visit.client), selectinload(Visit.services))
        .where(Visit.is_cancelled.is_(False), Visit.performed_date >= day_start, Visit.performed_date < day_end)
        .order_by(Visit.performed_date.asc(), Visit.id.asc())
    )
    if is_master:
        v_stmt = v_stmt.where(
            or_(
                Visit.id.in_(select(VisitMaster.visit_id).where(VisitMaster.master_id == current_user.id)),
                Visit.id.in_(
                    select(VisitService.visit_id).where(
                        VisitService.is_cancelled.is_(False),
                        VisitService.mix_bonus_master_id == current_user.id,
                    )
                ),
                Visit.id.in_(
                    select(VisitService.visit_id)
                    .join(VisitServiceMaster, VisitServiceMaster.visit_service_id == VisitService.id)
                    .where(
                        VisitService.is_cancelled.is_(False),
                        VisitServiceMaster.master_id == current_user.id,
                    )
                ),
                Visit.mix_bonus_master_id == current_user.id,
            )
        )
    visits = list(db.scalars(v_stmt).all())
    vs_ids: list[int] = []
    for v in visits:
        for s in v.services or []:
            if not s.is_cancelled and s.id:
                vs_ids.append(int(s.id))
    visit_ids = [int(v.id) for v in visits if v.id is not None]
    visit_payout_vs = _sum_ledger(
        db,
        side=PayrollFundSide.MASTER,
        source_kind=PayrollFundSourceKind.VISIT_SERVICE,
        source_ids=vs_ids,
        user_id=current_user.id if is_master else None,
    )
    visit_payout_legacy = _sum_ledger(
        db,
        side=PayrollFundSide.MASTER,
        source_kind=PayrollFundSourceKind.VISIT,
        source_ids=visit_ids,
        user_id=current_user.id if is_master else None,
    )
    visit_studio_vs = (
        _sum_ledger(
            db,
            side=PayrollFundSide.STUDIO,
            source_kind=PayrollFundSourceKind.VISIT_SERVICE,
            source_ids=vs_ids,
            user_id=None,
        )
        if is_super
        else {}
    )
    visit_studio_legacy = (
        _sum_ledger(
            db,
            side=PayrollFundSide.STUDIO,
            source_kind=PayrollFundSourceKind.VISIT,
            source_ids=visit_ids,
            user_id=None,
        )
        if is_super
        else {}
    )

    visit_items: list[dict[str, Any]] = []
    for v in visits:
        vid = int(v.id)
        client_name = v.client.name if v.client else "—"
        active_services = sorted(
            [s for s in (v.services or []) if not s.is_cancelled],
            key=lambda x: (int(x.sort_order or 0), int(x.id or 0)),
        )
        if not active_services:
            visit_items.append(
                {
                    "id": vid,
                    "client": client_name,
                    "label": "—",
                    "service_label": "",
                    "url": f"/visits/{vid}",
                    "payout_sum": float(visit_payout_legacy.get(vid, 0.0)),
                    "studio_sum": float(visit_studio_legacy.get(vid, 0.0)),
                }
            )
            continue
        legacy_payout_allocated = False
        legacy_studio_allocated = False
        for svc in active_services:
            if is_master:
                show = svc.mix_bonus_master_id == current_user.id
                if not show and v.masters_scope == VisitMastersScope.VISIT:
                    vm_ids = list(
                        db.scalars(select(VisitMaster.master_id).where(VisitMaster.visit_id == vid)).all()
                    )
                    show = current_user.id in vm_ids
                if not show:
                    mids = list(
                        db.scalars(
                            select(VisitServiceMaster.master_id).where(
                                VisitServiceMaster.visit_service_id == svc.id
                            )
                        ).all()
                    )
                    show = current_user.id in mids
                if not show:
                    continue
            sid = int(svc.id)
            payout = float(visit_payout_vs.get(sid, 0.0))
            studio = float(visit_studio_vs.get(sid, 0.0))
            if not legacy_payout_allocated:
                payout += float(visit_payout_legacy.get(vid, 0.0))
                legacy_payout_allocated = True
            if is_super and not legacy_studio_allocated:
                studio += float(visit_studio_legacy.get(vid, 0.0))
                legacy_studio_allocated = True
            visit_items.append(
                {
                    "id": vid,
                    "client": client_name,
                    "label": svc.service_name,
                    "service_label": format_visit_service_catalog_path(svc),
                    "url": f"/visits/{vid}",
                    "payout_sum": payout,
                    "studio_sum": studio,
                }
            )

    # ---- Works ----
    w_day = func.coalesce(WorkForInventory.performed_date, WorkForInventory.created_at)
    w_stmt = (
        select(WorkForInventory)
        .options(selectinload(WorkForInventory.client))
        .where(WorkForInventory.is_voided.is_(False), w_day >= day_start, w_day < day_end)
        .order_by(w_day.asc(), WorkForInventory.id.asc())
    )
    if is_master:
        w_stmt = w_stmt.where(
            or_(
                WorkForInventory.created_by_user_id == current_user.id,
                WorkForInventory.id.in_(
                    select(WorkForInventoryStaff.work_id).where(WorkForInventoryStaff.user_id == current_user.id)
                ),
            )
        )
    works = list(db.scalars(w_stmt).all())
    work_ids = [int(w.id) for w in works if w.id is not None]
    work_payout = _sum_ledger(
        db,
        side=PayrollFundSide.MASTER,
        source_kind=PayrollFundSourceKind.WORK,
        source_ids=work_ids,
        user_id=current_user.id if is_master else None,
    )
    work_studio = (
        _sum_ledger(
            db,
            side=PayrollFundSide.STUDIO,
            source_kind=PayrollFundSourceKind.WORK,
            source_ids=work_ids,
            user_id=None,
        )
        if is_super
        else {}
    )

    from app.visit_draft import draft_summary_label, drafts_for_calendar_day, preview_dict_from_json

    draft_items: list[dict[str, Any]] = []
    for dr in drafts_for_calendar_day(db, user=current_user, day=day):
        preview = preview_dict_from_json(dr.preview_json)
        draft_items.append(
            {
                "id": int(dr.id),
                "client": dr.client.name if dr.client else "—",
                "label": draft_summary_label(preview),
                "url": f"/master/visit/draft/{int(dr.id)}",
                "payout_sum": 0.0,
                "studio_sum": 0.0,
            }
        )

    work_items: list[dict[str, Any]] = []
    for w in works:
        wid = int(w.id)
        work_items.append(
            {
                "id": wid,
                "client": (w.client.name if w.client else "—"),
                "label": _work_activity_label(w).replace("Работа: ", ""),
                "url": f"/sales/work/{wid}",
                "payout_sum": float(work_payout.get(wid, 0.0)),
                "studio_sum": float(work_studio.get(wid, 0.0)),
            }
        )

    hour_from, hour_to = get_calendar_display_hours(db)
    occupancy = build_occupancy_for_day(
        db, day=day, hour_from=hour_from, hour_to=hour_to, bookings=bookings
    )

    if (view or "").strip().lower() == "occupancy":
        return JSONResponse(
            {
                "date": day.isoformat(),
                "occupancy": occupancy,
                "is_super": is_super,
                "view": "occupancy",
            }
        )

    resp = {
        "date": day.isoformat(),
        "occupancy": occupancy,
        "bookings": {"count": len(booking_items), "payout_sum": 0.0, "studio_sum": 0.0, "items": booking_items},
        "visits": {
            "count": len(visit_items),
            "payout_sum": _money0(sum(i["payout_sum"] for i in visit_items)),
            "studio_sum": _money0(sum(i["studio_sum"] for i in visit_items)) if is_super else 0.0,
            "items": visit_items,
        },
        "works": {
            "count": len(work_items),
            "payout_sum": _money0(sum(i["payout_sum"] for i in work_items)),
            "studio_sum": _money0(sum(i["studio_sum"] for i in work_items)) if is_super else 0.0,
            "items": work_items,
        },
        "drafts": {
            "count": len(draft_items),
            "payout_sum": 0.0,
            "studio_sum": 0.0,
            "items": draft_items,
        },
        "is_super": is_super,
    }
    return JSONResponse(resp)


@router.get("/api/booking/available-masters")
def api_booking_available_masters(
    d: str,
    t: str,
    service_id: int,
    duration_minutes: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    """
    Для админ-формы брони: какие мастера доступны по графику на выбранную дату/время
    (интервал = duration из estimated_duration_minutes).
    """
    try:
        day = date.fromisoformat((d or "").strip())
    except Exception:
        raise HTTPException(status_code=400, detail="bad date")

    time_raw = (t or "").strip()
    try:
        parts = time_raw.replace(".", ":").split(":")
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        tm = time(hour=hh % 24, minute=min(max(mm, 0), 59))
    except Exception:
        raise HTTPException(status_code=400, detail="bad time")

    svc = db.get(Service, int(service_id))
    if svc is None:
        raise HTTPException(status_code=400, detail="service not found")

    dur_min = int(duration_minutes or 0) or int(svc.estimated_duration_minutes or 0)
    if dur_min <= 0:
        return JSONResponse({"available_master_ids": []})

    start_dt = datetime.combine(day, tm)
    end_dt = start_dt + timedelta(minutes=dur_min)

    available: list[int] = []
    for m in list_calendar_masters(db):
        mid = int(m["id"])
        if is_master_available_for_interval(db, master_id=mid, start_dt=start_dt, end_dt=end_dt):
            available.append(mid)

    return JSONResponse({"available_master_ids": available})

