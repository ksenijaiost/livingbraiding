from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStaff,
    BookingStatus,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    UserRole,
    Visit,
    VisitMaster,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
)
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime
from app.display_time import get_display_timezone
from app.forms_parse import parse_date_iso


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
    return k


def _booking_status_label(s: str) -> str:
    if s == BookingStatus.ACTIVE.value:
        return "активна"
    if s == BookingStatus.DONE.value:
        return "✅ выполнена"
    if s == BookingStatus.CANCELLED.value:
        return "❌ отменена"
    return s


def _work_activity_label(w: WorkForInventory) -> str:
    scope = "В наличие" if w.scope == WorkScope.IN_STOCK else "На заказ"
    kind_map = {
        WorkKind.KIT: "комплект/заготовки",
        WorkKind.MIX: "смешка",
        WorkKind.RUBBER: "хвосты/резинки",
        WorkKind.KIT_CORRECTION: "коррекция комплекта",
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
    if not source_ids:
        return {}
    stmt = (
        select(PayrollFundLedger.source_id, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
        .where(
            PayrollFundLedger.side == side,
            PayrollFundLedger.source_kind == source_kind,
            PayrollFundLedger.source_id.in_(source_ids),
        )
        .group_by(PayrollFundLedger.source_id)
    )
    if user_id is not None:
        stmt = stmt.where(PayrollFundLedger.user_id == user_id)
    rows = list(db.execute(stmt).all())
    out: dict[int, float] = {}
    for sid, amt in rows:
        if sid is None:
            continue
        out[int(sid)] = _money0(float(amt or 0.0))
    return out


@router.get("/api/calendar/day")
def api_calendar_day(
    d: str,
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
        .options(selectinload(Booking.client))
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
        booking_items.append(
            {
                "id": int(b.id),
                "client": (b.client.name if b.client else "—"),
                "kind": kind_l,
                "label": f"{kind_l} · {time_l}",
                "status": _booking_status_label(b.status.value),
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
                Visit.mix_bonus_master_id == current_user.id,
            )
        )
    visits = list(db.scalars(v_stmt).all())
    visit_ids = [int(v.id) for v in visits if v.id is not None]
    visit_payout = _sum_ledger(
        db,
        side=PayrollFundSide.MASTER,
        source_kind=PayrollFundSourceKind.VISIT,
        source_ids=visit_ids,
        user_id=current_user.id if is_master else None,
    )
    visit_studio = (
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
        svc = None
        try:
            svc = (sorted(list(v.services or []), key=lambda x: int(x.id or 0))[:1] or [None])[0]
        except Exception:
            svc = None
        svc_name = (svc.service_name if svc else "—") if svc is not None else "—"
        vid = int(v.id)
        visit_items.append(
            {
                "id": vid,
                "client": (v.client.name if v.client else "—"),
                "label": svc_name,
                "url": f"/visits/{vid}",
                "payout_sum": float(visit_payout.get(vid, 0.0)),
                "studio_sum": float(visit_studio.get(vid, 0.0)),
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

    resp = {
        "date": day.isoformat(),
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
        "is_super": is_super,
    }
    return JSONResponse(resp)

