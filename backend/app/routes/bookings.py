from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile
from sqlalchemy import and_, case, delete, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.audit import FieldChange, diff_fields, write_audit_rows

_logger = logging.getLogger("livingbraiding.bookings")
from app.auth import AuthUser, require_role
from app.client_validation import format_created_by_label, strip_or_none
from app.consultation_booking import (
    booking_is_open,
    booking_status_label,
    can_create_booking_from_consultation,
    can_create_consultation_from_booking,
    consultation_for_booking,
)
from app.consultation_types import (
    CONSULTATION_TYPE_CHOICES,
    list_consultation_services_catalog,
    parse_types_from_form,
    types_json_dumps,
    types_json_loads,
)
from app.db.models import (
    Booking,
    BookingAuditLog,
    BookingKind,
    BookingMaster,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    BookingStaff,
    BookingStaffKind,
    BookingStatus,
    Client,
    Consultation,
    PayrollFundSourceKind,
    Kit,
    KitAuditLog,
    KitReserve,
    ProductSale,
    ProductSaleKind,
    User,
    UserRole,
    Visit,
    VisitMaster,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
    Service,
    ServiceSubcategory,
)
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.planned_service_time import planned_start_local_datetime
from app.forms_parse import parse_bool, parse_float, parse_int
from app.kit_blank_stock_core import (
    blank_stock_qty_map,
    build_usage_breakdown_keyed,
    consume_blank_stock_for_reserve,
    kit_inventory_is_keyed,
    max_take_by_key_for_client,
    return_reserve_row_to_stock,
    sync_kit_pieces_available_from_blank_lines,
)
from app.kit_inlay_visit import (
    get_kit_max_reserves_per_kit,
    kit_reserve_slots_used,
    list_master_visit_services_catalog,
    service_requires_kit_block,
)
from app.media_store import delete_media_by_url, get_nonempty_upload, save_upload_image
from app.time_utils import utcnow_naive
from app.user_roles import select_users_with_any_role, select_users_with_role
from app.work_products import _rubber_type_items
from app.master_schedule import is_master_available_for_interval
from app.ui_service_display import booking_list_detail_parts
from app.webui import templates, ctx as _ctx


router = APIRouter(prefix="/bookings", tags=["bookings"])
# GET-алиас: /admin/bookings/... -> 308 -> /bookings/...
legacy_bookings_admin_router = APIRouter(prefix="/admin/bookings", tags=["bookings-legacy"])
master_bookings_page_router = APIRouter(prefix="/master/bookings", tags=["bookings-master"])


def _redirect_admin_bookings_to_canon(request: Request, *, suffix: str = "") -> RedirectResponse:
    suf = (suffix or "").strip()
    if suf and not suf.startswith("/"):
        suf = f"/{suf}"
    new_path = f"/bookings{suf}"
    return RedirectResponse(url=str(request.url.replace(path=new_path)), status_code=308)


_BOOKINGS_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_BOOKINGS_ADMINS = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER))


def _booking_kind_label(k: str) -> str:
    if k == BookingKind.VISIT.value:
        return "Визит (услуга)"
    if k == BookingKind.PRODUCT_SALE.value:
        return "Продажа (без услуги)"
    if k == BookingKind.CONSULTATION.value:
        return "Консультация"
    return k


_BOOKING_KIND_OPTIONS = [
    BookingKind.VISIT.value,
    BookingKind.PRODUCT_SALE.value,
    BookingKind.CONSULTATION.value,
]


def _booking_status_label(s: str) -> str:
    try:
        return booking_status_label(BookingStatus(s))
    except ValueError:
        return s


def _apply_super_admin_booking_status_change(
    db: Session,
    b: Booking,
    new_status: BookingStatus,
    editor_user_id: int,
) -> None:
    """Смена статуса брони суперадмином при редактировании (в любую сторону)."""
    old_status = b.status
    if new_status == old_status:
        return
    if new_status == BookingStatus.CANCELLED:
        b.status = BookingStatus.CANCELLED
        b.cancelled_at = utcnow_naive()
        b.cancelled_by_user_id = editor_user_id
        if not (b.cancelled_reason or "").strip():
            b.cancelled_reason = "Статус изменён суперадмином"
        if b.consultation_id:
            from app.payroll_fund import storno_source_accruals

            storno_source_accruals(
                db,
                PayrollFundSourceKind.CONSULTATION,
                int(b.consultation_id),
                editor_user_id,
            )
            b.consultation_id = None
        return
    if old_status == BookingStatus.CANCELLED:
        b.cancelled_at = None
        b.cancelled_by_user_id = None
        b.cancelled_reason = None
    b.status = new_status


def _product_kind_label(k: str | None) -> str:
    if not k:
        return "—"
    return {
        ProductSaleKind.MATERIAL.value: "Материал",
        ProductSaleKind.KIT.value: "Комплект",
        ProductSaleKind.RUBBER.value: "Хвост/резинка",
        ProductSaleKind.OTHER.value: "Другое",
    }.get(k, k)


def _canonical_booking_details_json(raw: str | None) -> str | None:
    """Стабильное строковое представление JSON для сравнения в аудите (порядок ключей не важен)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False, sort_keys=True)
        return s
    except Exception:
        return s


def _audit_user_names(db: Session, user_ids: list[int]) -> str:
    ids = sorted(set([int(x) for x in user_ids if int(x) > 0]))
    if not ids:
        return "—"
    rows = list(db.scalars(select(User).where(User.id.in_(ids))).all())
    by_id = {int(u.id): (u.display_name or f"#{u.id}") for u in rows}
    out = [by_id.get(i, f"#{i}") for i in ids]
    return ", ".join(out) if out else "—"


def _parse_ids_csv(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for p in str(raw).replace(" ", "").split(","):
        if not p:
            continue
        try:
            out.append(parse_int(p, min=1, field_name="ids_csv"))
        except ValueError:
            continue
    return sorted(set([i for i in out if i > 0]))


def _pretty_user_ids_csv(db: Session, raw: str | None) -> str:
    ids = _parse_ids_csv(raw)
    return _audit_user_names(db, ids)


def _parse_line_duration_minutes(raw: Any) -> int:
    if raw is None or raw == "":
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _planned_services_audit_label(db: Session, booking_id: int) -> str:
    rows = list(
        db.scalars(
            select(BookingPlannedService)
            .where(BookingPlannedService.booking_id == booking_id)
            .options(selectinload(BookingPlannedService.service))
            .order_by(BookingPlannedService.sort_order.asc(), BookingPlannedService.id.asc())
        ).all()
    )
    if not rows:
        return "—"
    tz = get_display_timezone(db)
    booking = db.get(Booking, booking_id)
    parts: list[str] = []
    for ps in rows:
        svc = ps.service
        name = (svc.name if svc else f"#{ps.service_id}")
        dur = int(ps.duration_minutes or 0)
        if dur <= 0 and svc is not None:
            dur = int(svc.estimated_duration_minutes or 0)
        if ps.planned_start_time:
            local_start = planned_start_local_datetime(
                ps.planned_start_time,
                booking_planned_date=booking.planned_date if booking else None,
                tz_name=tz,
            )
            t_s = local_start.strftime("%H:%M") if local_start else "?"
        else:
            t_s = "?"
        parts.append(f"{name} {t_s} ({dur} мин)")
    return "; ".join(parts)


def _audit_sale_order_masters_label(db: Session, booking_id: int) -> str:
    staff = list(
        db.scalars(
            select(BookingStaff).where(
                BookingStaff.booking_id == booking_id,
                BookingStaff.kind.in_([BookingStaffKind.SALE_KIT_ORDER, BookingStaffKind.SALE_RUBBER_ORDER]),
            )
        ).all()
    )
    kit_ids = [int(r.user_id) for r in staff if r.kind == BookingStaffKind.SALE_KIT_ORDER]
    rub_ids = [int(r.user_id) for r in staff if r.kind == BookingStaffKind.SALE_RUBBER_ORDER]
    parts: list[str] = []
    if kit_ids:
        parts.append(f"Комплект: {_audit_user_names(db, kit_ids)}")
    if rub_ids:
        parts.append(f"Хвост/резинка: {_audit_user_names(db, rub_ids)}")
    return "; ".join(parts) if parts else "—"


_DETAILS_KIT_DEFAULT_BACKFILL: dict[str, set[str]] = {
    "visit_kit_mode": {"IN_STOCK"},
    "visit_extra_blanks_mode": {"IN_STOCK"},
}


def _apply_parsed_visit_lines_to_fp(fp: dict[str, str], line_specs: list[dict[str, Any]]) -> None:
    """Синхронизируем fp с распарсенными строками услуг (для details_json и скрытых полей)."""
    if not line_specs:
        return
    fp["service_id"] = str(int(line_specs[0]["service_id"]))
    fp["planned_service_ids"] = json.dumps(
        [int(x["service_id"]) for x in line_specs],
        ensure_ascii=False,
    )
    line_payload: list[dict[str, Any]] = []
    for spec in line_specs:
        dur = _parse_line_duration_minutes(spec.get("duration_minutes"))
        t0 = spec.get("planned_time")
        t_s = t0.strftime("%H:%M") if isinstance(t0, time) else str(fp.get("planned_time") or "")
        mst: dict[str, str] = {}
        raw_mst = spec.get("master_start_times")
        if isinstance(raw_mst, dict):
            for mid, tm in raw_mst.items():
                try:
                    mid_i = int(mid)
                except Exception:
                    continue
                if mid_i <= 0 or not isinstance(tm, time):
                    continue
                mst[str(mid_i)] = tm.strftime("%H:%M")
        item: dict[str, Any] = {
            "service_id": int(spec["service_id"]),
            "planned_time": t_s,
            "master_ids": [int(x) for x in (spec.get("master_ids") or []) if int(x) > 0],
            "comment": str(spec.get("comment") or ""),
        }
        if mst:
            item["master_start_times"] = mst
        if dur > 0:
            item["duration_minutes"] = dur
        line_payload.append(item)
    fp["booking_service_lines_json"] = json.dumps(line_payload, ensure_ascii=False)
    if len(line_specs) == 1:
        dur0 = _parse_line_duration_minutes(line_specs[0].get("duration_minutes"))
        if dur0 > 0:
            fp["visit_custom_duration_on"] = "1"
            fp["visit_custom_duration_h"] = str(dur0 // 60)
            fp["visit_custom_duration_m"] = str(dur0 % 60)
        else:
            fp.pop("visit_custom_duration_on", None)
            fp.pop("visit_custom_duration_h", None)
            fp.pop("visit_custom_duration_m", None)


def _booking_details_audit_changes(db: Session, before_raw: str | None, after_raw: str | None) -> list[FieldChange]:
    """В аудит пишем изменения по ключам details_json (а не весь JSON)."""

    def _load(s: str | None) -> dict[str, Any]:
        if not s:
            return {}
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    before = _load(before_raw)
    after = _load(after_raw)
    keys = sorted(set(list(before.keys()) + list(after.keys())))
    out: list[FieldChange] = []
    for k in keys:
        a = before.get(k)
        b = after.get(k)
        if a == b:
            continue
        if a is None and b is not None and str(b) in _DETAILS_KIT_DEFAULT_BACKFILL.get(k, set()):
            continue
        if k in ("sale_kit_order_master_ids", "visit_order_master_ids"):
            out.append(
                FieldChange(
                    field_name=str(k),
                    old_value=_pretty_user_ids_csv(db, str(a) if a is not None else None),
                    new_value=_pretty_user_ids_csv(db, str(b) if b is not None else None),
                )
            )
            continue
        if k in ("sale_kit_order_master_ids", "visit_order_master_ids", "sale_rubber_order_master_id"):
            continue
        out.append(FieldChange(field_name=str(k), old_value=None if a is None else str(a), new_value=None if b is None else str(b)))
    return out


def _refresh_sale_order_master_ids_in_fp(db: Session, *, booking_id: int, fp: dict[str, str]) -> None:
    """Держим *_master_ids в fp синхронно с booking_staff (источник истины для назначений)."""
    b = db.get(Booking, int(booking_id))
    if b is None:
        fp.pop("sale_kit_order_master_ids", None)
        fp.pop("sale_rubber_order_master_id", None)
        fp.pop("visit_order_master_ids", None)
        fp.pop("visit_order_use_masters", None)
        return
    staff = list(
        db.scalars(
            select(BookingStaff).where(
                BookingStaff.booking_id == booking_id,
                BookingStaff.kind.in_([BookingStaffKind.SALE_KIT_ORDER, BookingStaffKind.SALE_RUBBER_ORDER]),
            )
        ).all()
    )
    kit_ids = sorted(
        set([int(r.user_id) for r in staff if r.kind == BookingStaffKind.SALE_KIT_ORDER and int(r.user_id) > 0])
    )
    rub_id = next(
        (int(r.user_id) for r in staff if r.kind == BookingStaffKind.SALE_RUBBER_ORDER and int(r.user_id) > 0),
        None,
    )
    if b.kind == BookingKind.PRODUCT_SALE:
        if kit_ids:
            fp["sale_kit_order_master_ids"] = ",".join([str(i) for i in kit_ids])
        else:
            fp.pop("sale_kit_order_master_ids", None)
        if rub_id:
            fp["sale_rubber_order_master_id"] = str(rub_id)
        else:
            fp.pop("sale_rubber_order_master_id", None)
        fp.pop("visit_order_master_ids", None)
        fp.pop("visit_order_use_masters", None)
    elif b.kind == BookingKind.VISIT:
        if kit_ids:
            fp["visit_order_master_ids"] = ",".join([str(i) for i in kit_ids])
            fp["visit_order_use_masters"] = "1"
        else:
            fp.pop("visit_order_master_ids", None)
            fp.pop("visit_order_use_masters", None)
        fp.pop("sale_kit_order_master_ids", None)
        fp.pop("sale_rubber_order_master_id", None)
    else:
        fp.pop("sale_kit_order_master_ids", None)
        fp.pop("sale_rubber_order_master_id", None)
        fp.pop("visit_order_master_ids", None)
        fp.pop("visit_order_use_masters", None)


def _utc_naive_to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def _local_naive_to_utc_naive(dt: datetime, tz_name: str) -> datetime:
    local_dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return local_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _parse_planned_booking_datetime(fp: dict[str, str], tz_name: str) -> datetime:
    date_raw = str(fp.get("planned_date") or "").strip()
    time_raw = str(fp.get("planned_time") or "").strip()
    if not date_raw:
        raise ValueError("planned_date required")
    d = datetime.strptime(date_raw, "%Y-%m-%d")
    d = d.replace(second=0, microsecond=0)
    if not time_raw:
        raise ValueError("planned_time required")
    parts = time_raw.replace(".", ":").split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        raise ValueError("planned_time invalid")
    if h < 0 or h > 23 or m < 0 or m > 59:
        raise ValueError("planned_time invalid")
    d = d.replace(hour=h, minute=m)
    return _local_naive_to_utc_naive(d, tz_name).replace(second=0, microsecond=0)


def _booking_form_prefill_from_db(db: Session, b: Booking) -> tuple[dict[str, str], list[int]]:
    tz = get_display_timezone(db)
    local_dt = _utc_naive_to_local(b.planned_date, tz) if b.planned_date else None
    fp: dict[str, str] = {
        "client_id": str(b.client_id),
        "planned_date": local_dt.date().isoformat() if local_dt else "",
        "planned_time": local_dt.strftime("%H:%M") if local_dt else "",
        "kind": b.kind.value if b.kind else BookingKind.VISIT.value,
        "quoted_price_text": b.quoted_price_text or "",
        "deposit_amount": "" if b.deposit_amount is None else str(int(b.deposit_amount)),
        "comment": b.comment or "",
        "booking_status": b.status.value if b.status else BookingStatus.PENDING_CONFIRMATION.value,
    }
    if b.kind == BookingKind.VISIT:
        service_ids_for_hidden: list[int] = []
        line_payload: list[dict[str, Any]] = []
        details_dur = 0
        if b.details_json:
            try:
                d0 = json.loads(b.details_json)
                if isinstance(d0, dict):
                    details_dur = int(d0.get("visit_custom_duration_minutes") or 0)
            except Exception:
                details_dur = 0
        if details_dur < 0:
            details_dur = 0
        lines = list(b.planned_services or [])
        if lines:
            sorted_lines = sorted(lines, key=lambda x: (int(x.sort_order or 0), int(x.id or 0)))
            for i, ps in enumerate(sorted_lines):
                sid = int(ps.service_id or 0)
                if sid <= 0:
                    continue
                service_ids_for_hidden.append(sid)
                local_start = (
                    planned_start_local_datetime(
                        ps.planned_start_time,
                        booking_planned_date=b.planned_date,
                        tz_name=tz,
                    )
                    if ps.planned_start_time
                    else None
                )
                dur = int(ps.duration_minutes or 0)
                if dur <= 0 and i == 0 and details_dur > 0:
                    dur = details_dur
                line_item: dict[str, Any] = {
                    "service_id": sid,
                    "planned_time": local_start.strftime("%H:%M") if local_start else fp.get("planned_time", ""),
                    "master_ids": [int(x.master_id) for x in (ps.masters or []) if x.master_id],
                    "comment": (ps.comment or ""),
                }
                master_start_map: dict[str, str] = {}
                for psm in (ps.masters or []):
                    if not psm.master_id or not psm.planned_start_time:
                        continue
                    m_local = planned_start_local_datetime(
                        psm.planned_start_time,
                        booking_planned_date=b.planned_date,
                        tz_name=tz,
                    )
                    if m_local is None:
                        continue
                    master_start_map[str(int(psm.master_id))] = m_local.strftime("%H:%M")
                if master_start_map:
                    line_item["master_start_times"] = master_start_map
                if dur > 0:
                    line_item["duration_minutes"] = dur
                line_payload.append(line_item)
        if not service_ids_for_hidden and b.planned_service_id:
            service_ids_for_hidden = [int(b.planned_service_id)]
            line_payload = [
                {
                    "service_id": int(b.planned_service_id),
                    "planned_time": fp.get("planned_time", ""),
                    "master_ids": [],
                    "comment": "",
                }
            ]
        fp["service_id"] = str(service_ids_for_hidden[0] if service_ids_for_hidden else "")
        if service_ids_for_hidden:
            fp["planned_service_ids"] = json.dumps(service_ids_for_hidden, ensure_ascii=False)
            fp["booking_service_lines_json"] = json.dumps(line_payload, ensure_ascii=False)
            booking_master_set = set([int(x.master_id) for x in (b.masters or []) if x.master_id])
            all_same_as_booking = bool(line_payload)
            for x in line_payload:
                mids = set([int(v) for v in (x.get("master_ids") or []) if int(v) > 0])
                if mids != booking_master_set:
                    all_same_as_booking = False
                    break
            fp["booking_masters_mode"] = "all" if all_same_as_booking else "per_service"

        if service_ids_for_hidden:
            svc = db.get(Service, service_ids_for_hidden[0])
            if svc:
                sub = db.get(ServiceSubcategory, svc.subcategory_id)
                if sub:
                    fp["service_subcategory_id"] = str(sub.id)
                    fp["service_category_id"] = str(sub.category_id)
    elif b.kind == BookingKind.PRODUCT_SALE:
        fp["product_kind"] = b.planned_product_kind or ""
    elif b.kind == BookingKind.CONSULTATION:
        from app.planned_services_db import booking_planned_service_ids

        svc_ids = booking_planned_service_ids(db, b.id)
        if svc_ids:
            fp["planned_service_ids"] = json.dumps(svc_ids, ensure_ascii=False)
    if b.details_json:
        try:
            d = json.loads(b.details_json)
            if isinstance(d, dict):
                for k, v in d.items():
                    if v is None:
                        continue
                    fp[str(k)] = str(v) if not isinstance(v, bool) else ("1" if v else "")
        except Exception:
            pass
    _expand_line_service_kits_to_fp(fp)
    master_ids = [bm.master_id for bm in (b.masters or [])]
    if not master_ids:
        for ps in (b.planned_services or []):
            for psm in (ps.masters or []):
                if psm.master_id and psm.master_id not in master_ids:
                    master_ids.append(int(psm.master_id))
    return fp, master_ids


def _booking_details_flag(v: Any) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def booking_linked_need_work(b: Booking, details: dict[str, Any]) -> bool:
    if b.kind == BookingKind.VISIT:
        mode = str(details.get("visit_kit_mode") or "").strip().upper()
        if mode == "ORDER":
            return True
        if mode == "OWN" and _booking_details_flag(details.get("visit_own_need_correction")):
            return True
        return False
    if b.kind == BookingKind.PRODUCT_SALE:
        pk = (b.planned_product_kind or "").strip().upper()
        if pk == "KIT" and str(details.get("sale_kit_mode") or "").strip().upper() == "ORDER":
            return True
        if pk == "RUBBER" and str(details.get("sale_rubber_mode") or "").strip().upper() == "ORDER":
            return True
    return False


def booking_linked_need_visit(b: Booking) -> bool:
    return b.kind == BookingKind.VISIT


def booking_linked_need_sale(b: Booking) -> bool:
    return b.kind == BookingKind.PRODUCT_SALE


def _prefill_booking_fp_from_consultation(db: Session, cons: Consultation, fp: dict[str, str]) -> str | None:
    """Заполнить fp из консультации. Возвращает текст ошибки или None."""
    from app.planned_services_db import consultation_service_ids

    fp["client_id"] = str(cons.client_id)
    if cons.preliminary_cost_text:
        fp["quoted_price_text"] = cons.preliminary_cost_text
    svc_ids = consultation_service_ids(db, cons.id)
    if svc_ids:
        import json as _json

        fp["planned_service_ids"] = _json.dumps(svc_ids)
        fp["service_id"] = str(svc_ids[0])
        fp["booking_service_lines_json"] = _json.dumps(
            [
                {
                    "service_id": int(sid),
                    "planned_time": fp.get("planned_time", ""),
                    "master_ids": [],
                }
                for sid in svc_ids
            ],
            ensure_ascii=False,
        )
        fp["booking_masters_mode"] = "all"
        fp["kind"] = BookingKind.VISIT.value
        svc = db.get(Service, svc_ids[0])
        if svc:
            sub = db.get(ServiceSubcategory, svc.subcategory_id)
            if sub:
                fp["service_subcategory_id"] = str(sub.id)
                fp["service_category_id"] = str(sub.category_id)
    elif cons.service_id:
        fp["service_id"] = str(cons.service_id)
        fp["kind"] = BookingKind.VISIT.value
        svc = db.get(Service, cons.service_id)
        if svc:
            sub = db.get(ServiceSubcategory, svc.subcategory_id)
            if sub:
                fp["service_subcategory_id"] = str(sub.id)
                fp["service_category_id"] = str(sub.category_id)
    if cons.photo_1:
        fp["prefill_photo_1"] = cons.photo_1
    if cons.photo_2:
        fp["prefill_photo_2"] = cons.photo_2
    return None


def _parse_booking_consultation_fields(
    db: Session, form_raw: Any, fp: dict[str, str]
) -> tuple[str | None, dict[str, Any], list[int]]:
    """Вид и услуги консультации для брони типа CONSULTATION (оба необязательны)."""
    from app.planned_services_db import parse_service_ids_from_form

    types_list = form_raw.getlist("consultation_types") if hasattr(form_raw, "getlist") else []
    types_data = parse_types_from_form(types_list, str(fp.get("other_text") or ""))
    service_ids = parse_service_ids_from_form(form_raw)
    for sid in service_ids:
        if db.get(Service, sid) is None:
            return "Услуга не найдена.", types_data, []
    return None, types_data, service_ids


def _consultation_types_data_from_booking_details(details: dict[str, Any]) -> dict[str, Any]:
    raw = details.get("consultation_types_json")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return types_json_loads(str(raw))


def _booking_form_consultation_types_data(fp: dict[str, str], details: dict[str, Any] | None = None) -> dict[str, Any]:
    if details:
        data = _consultation_types_data_from_booking_details(details)
        if data:
            return data
    raw = str(fp.get("consultation_types_json") or "").strip()
    if raw:
        return types_json_loads(raw)
    types_list = [x.strip() for x in str(fp.get("consultation_types") or "").split(",") if x.strip()]
    return parse_types_from_form(types_list, str(fp.get("other_text") or ""))


def _booking_form_consultation_template_ctx(
    db: Session,
    fp: dict[str, str],
    booking: Booking | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if booking and booking.details_json:
        try:
            raw = json.loads(booking.details_json)
            if isinstance(raw, dict):
                details = raw
        except Exception:
            details = {}
    types_data = _booking_form_consultation_types_data(fp, details)
    svc_ids: list[int] = []
    raw_ids = str(fp.get("planned_service_ids") or "").strip()
    if raw_ids.startswith("["):
        try:
            parsed = json.loads(raw_ids)
            if isinstance(parsed, list):
                svc_ids = [int(x) for x in parsed if int(x) > 0]
        except Exception:
            svc_ids = []
    elif booking is not None:
        from app.planned_services_db import booking_planned_service_ids

        svc_ids = booking_planned_service_ids(db, booking.id)
    return {
        "consultation_service_catalog": list_consultation_services_catalog(db),
        "consultation_type_choices": CONSULTATION_TYPE_CHOICES,
        "consultation_types_data": types_data,
        "consultation_selected_service_ids": svc_ids,
    }


def _parse_booking_visit_planned_services(
    db: Session, form_raw: Any, fp: dict[str, str]
) -> tuple[str | None, list[int], int | None]:
    """Ошибка, список service_id, первый id для planned_service_id."""
    from app.planned_services_db import parse_service_ids_from_form

    ids = parse_service_ids_from_form(form_raw)
    svc_raw = str(fp.get("service_id") or "").strip()
    if not ids and svc_raw:
        try:
            ids = [parse_int(svc_raw, min=1, field_name="service_id")]
        except ValueError:
            ids = []
    if not ids:
        return "Выберите хотя бы одну услугу для брони визита.", [], None
    for sid in ids:
        if db.get(Service, sid) is None:
            return "Услуга не найдена.", [], None
    return None, ids, ids[0]


def _parse_hhmm_to_time(raw: str) -> time | None:
    s = str(raw or "").strip()
    if not s:
        return None
    parts = s.replace(".", ":").split(":")
    try:
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return None
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    hh = hh % 24
    mm = min(max(mm, 0), 59)
    return time(hour=hh, minute=mm)


def _parse_master_start_times_raw(item: dict[str, Any]) -> tuple[dict[int, time], bool]:
    raw_map = item.get("master_start_times")
    if raw_map in (None, "", {}):
        return {}, True
    if not isinstance(raw_map, dict):
        return {}, False
    out: dict[int, time] = {}
    for mk, mv in raw_map.items():
        try:
            mid = int(mk)
        except Exception:
            continue
        if mid <= 0:
            continue
        tm = _parse_hhmm_to_time(str(mv or "").strip())
        if tm is None:
            return {}, False
        out[mid] = tm
    return out, True


def _line_master_time_info(
    spec: dict[str, Any],
    *,
    local_day: date,
    duration_minutes: int,
) -> tuple[datetime, datetime, dict[int, datetime]]:
    """Окно услуги: общее окончание от самого раннего старта, индивидуальные старты по мастерам."""
    t0 = spec.get("planned_time")
    if not isinstance(t0, time):
        t0 = time(0, 0)
    mids = [int(x) for x in (spec.get("master_ids") or []) if int(x) > 0]
    raw_map = spec.get("master_start_times")
    map_t: dict[int, time] = raw_map if isinstance(raw_map, dict) else {}
    starts_t = [t0] + [tm for mid, tm in map_t.items() if mid in mids and isinstance(tm, time)]
    earliest_t = min(starts_t) if starts_t else t0
    start_dt = datetime.combine(local_day, earliest_t).replace(second=0, microsecond=0)
    end_dt = start_dt + timedelta(minutes=int(duration_minutes))
    master_start_dt: dict[int, datetime] = {}
    for mid in mids:
        tm = map_t.get(mid, t0)
        if not isinstance(tm, time):
            tm = t0
        master_start_dt[mid] = datetime.combine(local_day, tm).replace(second=0, microsecond=0)
    return start_dt, end_dt, master_start_dt


def _parse_booking_visit_lines_and_masters(
    db: Session, form_raw: Any, fp: dict[str, str]
) -> tuple[str | None, list[int], int | None, list[dict[str, Any]], list[int]]:
    """Парсим услуги/время/мастеров для брони визита."""
    base_time = str(fp.get("planned_time") or "").strip()
    lines_json_raw = str(fp.get("booking_service_lines_json") or "").strip()
    if not lines_json_raw and hasattr(form_raw, "get"):
        lines_json_raw = str(form_raw.get("booking_service_lines_json") or "").strip()
    lines_json: list[dict[str, Any]] = []
    if lines_json_raw:
        try:
            parsed = json.loads(lines_json_raw)
            if isinstance(parsed, list):
                lines_json = [x for x in parsed if isinstance(x, dict)]
        except Exception:
            lines_json = []

    line_specs: list[dict[str, Any]] = []
    if lines_json:
        for i, item in enumerate(lines_json):
            try:
                sid = parse_int(str(item.get("service_id") or "").strip(), min=1, field_name="service_id")
            except ValueError:
                continue
            t_raw = str(item.get("planned_time") or "").strip()
            if i == 0 and not t_raw:
                t_raw = base_time
            tm = _parse_hhmm_to_time(t_raw)
            if tm is None:
                return "Укажите время начала для каждой услуги.", [], None, [], []
            raw_master_starts, ok_mst = _parse_master_start_times_raw(item)
            if not ok_mst:
                return "Некорректное индивидуальное время начала мастера.", [], None, [], []
            mids_raw = item.get("master_ids")
            mids: list[int] = []
            if isinstance(mids_raw, list):
                for x in mids_raw:
                    try:
                        mid = int(x)
                    except Exception:
                        continue
                    if mid > 0 and mid not in mids:
                        mids.append(mid)
            line_specs.append(
                {
                    "service_id": sid,
                    "planned_time": tm,
                    "master_ids": mids,
                    "_raw_master_start_times": raw_master_starts,
                    "comment": str(item.get("comment") or "").strip(),
                    "duration_minutes": _parse_line_duration_minutes(item.get("duration_minutes")),
                }
            )
    else:
        svc_err, ids, _ = _parse_booking_visit_planned_services(db, form_raw, fp)
        if svc_err:
            return svc_err, [], None, [], []
        for i, sid in enumerate(ids):
            tm = _parse_hhmm_to_time(base_time if i == 0 else "")
            if tm is None:
                return "Укажите время начала для каждой услуги.", [], None, [], []
            line_specs.append(
                {
                    "service_id": sid,
                    "planned_time": tm,
                    "master_ids": [],
                    "_raw_master_start_times": {},
                    "comment": "",
                    "duration_minutes": 0,
                }
            )

    if not line_specs:
        return "Выберите хотя бы одну услугу для брони визита.", [], None, [], []

    for spec in line_specs:
        if db.get(Service, int(spec["service_id"])) is None:
            return "Услуга не найдена.", [], None, [], []

    masters_mode = str(fp.get("booking_masters_mode") or "all").strip().lower()
    if masters_mode not in ("all", "per_service"):
        masters_mode = "all"
    fp["booking_masters_mode"] = masters_mode

    on_ids: list[int] = []
    if masters_mode == "all":
        for v in form_raw.getlist("booking_master_on"):
            try:
                on_ids.append(parse_int(v, min=1, field_name="booking_master_on"))
            except Exception:
                pass
        on_ids = sorted(set([i for i in on_ids if i > 0]))
        if not on_ids:
            return "Выберите хотя бы одного мастера.", [], None, [], []
        for spec in line_specs:
            spec["master_ids"] = list(on_ids)
    else:
        union_ids: set[int] = set()
        for spec in line_specs:
            mids = sorted(set([int(x) for x in (spec.get("master_ids") or []) if int(x) > 0]))
            if not mids:
                return "Для каждой услуги выберите хотя бы одного мастера.", [], None, [], []
            spec["master_ids"] = mids
            union_ids.update(mids)
        on_ids = sorted(union_ids)

    service_ids = [int(spec["service_id"]) for spec in line_specs]
    for spec in line_specs:
        mids = [int(x) for x in (spec.get("master_ids") or []) if int(x) > 0]
        raw_map = spec.pop("_raw_master_start_times", {})
        map_t: dict[int, time] = raw_map if isinstance(raw_map, dict) else {}
        spec["master_start_times"] = {mid: map_t[mid] for mid in mids if mid in map_t}
    planned_service_id = service_ids[0] if service_ids else None
    return None, service_ids, planned_service_id, line_specs, on_ids


def _booking_local_day_from_fp(fp: dict[str, str], *, planned_date_utc: datetime | None, tz_name: str) -> date:
    raw = str(fp.get("planned_date") or "").strip()
    try:
        return date.fromisoformat(raw)
    except Exception:
        local_dt = _utc_naive_to_local(planned_date_utc, tz_name) if planned_date_utc else None
        return local_dt.date() if local_dt else date.today()


def _booking_custom_duration_override_minutes(fp: dict[str, str]) -> int:
    if not parse_bool(fp.get("visit_custom_duration_on")):
        return 0
    raw_h = str(fp.get("visit_custom_duration_h") or "").strip()
    raw_m = str(fp.get("visit_custom_duration_m") or "").strip()
    try:
        hh = int(parse_float(raw_h or "0", min=0.0, field_name="visit_custom_duration_h"))
    except Exception:
        hh = 0
    try:
        mm = int(parse_float(raw_m or "0", min=0.0, field_name="visit_custom_duration_m"))
    except Exception:
        mm = 0
    if mm < 0:
        mm = 0
    if mm > 59:
        mm = 59
    total = int(hh) * 60 + int(mm)
    return total if total > 0 else 0


def _validate_booking_visit_line_availability(
    db: Session,
    *,
    local_day: date,
    line_specs: list[dict[str, Any]],
    duration_override_minutes: int = 0,
) -> str | None:
    unavailable_names: list[str] = []
    seen_mid: set[int] = set()
    for spec in line_specs:
        sid = int(spec["service_id"])
        svc = db.get(Service, sid)
        if svc is None:
            return "Услуга не найдена."
        dur = int(svc.estimated_duration_minutes or 0)
        line_dur = int(spec.get("duration_minutes") or 0)
        if line_dur > 0:
            dur = line_dur
        elif duration_override_minutes > 0 and len(line_specs) == 1:
            dur = int(duration_override_minutes)
        if dur <= 0:
            continue
        start_dt, end_dt, master_start_dt = _line_master_time_info(
            spec,
            local_day=local_day,
            duration_minutes=dur,
        )
        for mid in [int(x) for x in (spec.get("master_ids") or []) if int(x) > 0]:
            mid_start_dt = master_start_dt.get(mid, start_dt)
            if mid_start_dt >= end_dt:
                return "Индивидуальное время мастера должно быть раньше общего окончания услуги."
            if is_master_available_for_interval(
                db,
                master_id=mid,
                start_dt=mid_start_dt,
                end_dt=end_dt,
            ):
                continue
            if mid in seen_mid:
                continue
            seen_mid.add(mid)
            u = db.get(User, mid)
            unavailable_names.append((u.display_name or u.username) if u else f"#{mid}")
    if unavailable_names:
        return "Следующие мастера недоступны по графику на выбранное время: " + ", ".join(unavailable_names)
    return None


def _sync_booking_planned_services_with_lines(
    db: Session,
    *,
    booking_id: int,
    local_day: date,
    tz_name: str,
    line_specs: list[dict[str, Any]],
) -> None:
    db.execute(delete(BookingPlannedService).where(BookingPlannedService.booking_id == booking_id))
    db.flush()
    for i, spec in enumerate(line_specs):
        sid = int(spec["service_id"])
        line_dur = _parse_line_duration_minutes(spec.get("duration_minutes"))
        start_local, _line_end_local, per_master_local = _line_master_time_info(
            spec,
            local_day=local_day,
            duration_minutes=line_dur if line_dur > 0 else 1,
        )
        start_utc = _local_naive_to_utc_naive(start_local, tz_name).replace(second=0, microsecond=0)
        ps = BookingPlannedService(
            booking_id=booking_id,
            service_id=sid,
            sort_order=i,
            planned_start_time=start_utc,
            duration_minutes=line_dur if line_dur > 0 else None,
            comment=str(spec.get("comment") or "").strip() or None,
        )
        db.add(ps)
        db.flush()
        for mid in sorted(set([int(x) for x in (spec.get("master_ids") or []) if int(x) > 0])):
            if db.get(User, mid) is None:
                continue
            mid_start_local = per_master_local.get(mid, start_local)
            mid_start_utc = _local_naive_to_utc_naive(mid_start_local, tz_name).replace(second=0, microsecond=0)
            db.add(
                BookingPlannedServiceMaster(
                    booking_planned_service_id=ps.id,
                    master_id=mid,
                    planned_start_time=(mid_start_utc if mid_start_utc != start_utc else None),
                )
            )


def try_auto_complete_booking(db: Session, booking_id: int) -> None:
    b = db.get(Booking, booking_id)
    if not b or not booking_is_open(b.status):
        return
    details: dict[str, Any] = {}
    if b.details_json:
        try:
            raw = json.loads(b.details_json or "{}")
            if isinstance(raw, dict):
                details = raw
        except Exception:
            pass
    if booking_linked_need_work(b, details):
        wid = db.scalar(
            select(WorkForInventory.id).where(
                WorkForInventory.booking_id == booking_id,
                WorkForInventory.is_voided.is_(False),
            ).limit(1)
        )
        if wid is None:
            return
    if booking_linked_need_visit(b):
        vid = db.scalar(
            select(Visit.id).where(
                Visit.booking_id == booking_id,
                Visit.is_cancelled.is_(False),
            ).limit(1)
        )
        if vid is None:
            return
    if booking_linked_need_sale(b):
        sid = db.scalar(
            select(ProductSale.id).where(
                ProductSale.booking_id == booking_id,
                ProductSale.is_voided.is_(False),
            ).limit(1)
        )
        if sid is None:
            return
    old_status = b.status
    b.status = BookingStatus.DONE
    b.updated_at = utcnow_naive()
    b.updated_by_user_id = None
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=None,
        changes=[
            FieldChange("status", _booking_status_label(old_status.value), _booking_status_label(BookingStatus.DONE.value))
        ],
    )
    release_booking_kit_reserves(db, booking_id=b.id, changed_by_user_id=None)

    if b.consultation_id:
        from app.payroll_fund import post_consultation_accrual

        post_consultation_accrual(db, int(b.consultation_id), created_by_user_id=None)


def _amount_hint_from_booking(b: Booking) -> str:
    if b.deposit_amount is not None and int(b.deposit_amount) > 0:
        return str(int(b.deposit_amount))
    raw = (b.quoted_price_text or "").strip()
    if not raw:
        return ""
    m = re.search(r"(\d[\d\s]{2,})", raw)
    if not m:
        return ""
    digits = re.sub(r"\D", "", m.group(1))
    return digits if digits else ""


def _visit_stock_kit_initial_for_form(db: Session, fp: dict[str, str] | None) -> dict[str, Any] | None:
    if not fp:
        return None
    raw = str(fp.get("visit_stock_kit_id") or "").strip()
    if not raw.isdigit():
        return None
    kit = db.get(Kit, int(raw))
    if kit is None:
        return None
    return {
        "id": int(kit.id),
        "sku": str(kit.sku or ""),
        "title": str(kit.title or ""),
        "pieces_available": int(kit.pieces_available or 0),
    }


def _visit_stock_kit_from_details(db: Session, details: dict[str, Any]) -> Kit | None:
    raw = str(details.get("visit_stock_kit_id") or "").strip()
    if not raw.isdigit():
        return None
    return db.get(Kit, int(raw))


def _prefill_visit_stock_kit_from_booking(db: Session, b: Booking, form_prefill: dict[str, str]) -> None:
    vs = (form_prefill.get("visit_stock_kit_id") or "").strip()
    if vs.isdigit() and not (form_prefill.get("stock_kit_id") or "").strip():
        form_prefill["stock_kit_id"] = vs
    vp = (form_prefill.get("visit_stock_kit_pieces") or "").strip()
    if vp.isdigit() and not (form_prefill.get("stock_blanks_used") or "").strip():
        form_prefill["stock_blanks_used"] = vp
    if (form_prefill.get("stock_kit_id") or "").strip().isdigit():
        return
    if b.kind != BookingKind.VISIT:
        return
    work = db.scalar(
        select(WorkForInventory)
        .where(
            WorkForInventory.booking_id == b.id,
            WorkForInventory.is_voided.is_(False),
            WorkForInventory.kind == WorkKind.KIT,
            WorkForInventory.created_kit_id.isnot(None),
        )
        .order_by(WorkForInventory.id.desc())
        .limit(1)
    )
    if not work or not work.created_kit_id:
        return
    kid = int(work.created_kit_id)
    form_prefill["stock_kit_id"] = str(kid)
    if (form_prefill.get("stock_blanks_used") or "").strip().isdigit():
        return
    total_r = db.scalar(
        select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
            KitReserve.kit_id == kid,
            KitReserve.reserved_for_client_id == b.client_id,
        )
    )
    total_r = int(total_r or 0)
    if total_r > 0:
        form_prefill["stock_blanks_used"] = str(total_r)
    else:
        kit = db.get(Kit, kid)
        if kit and int(kit.pieces_total or 0) > 0:
            form_prefill["stock_blanks_used"] = str(int(kit.pieces_total))


def _booking_work_new_query_params(db: Session, b: Booking, details: dict[str, Any]) -> dict[str, str]:
    q: dict[str, str] = {"booking_id": str(b.id), "client_id": str(b.client_id)}
    tz = get_display_timezone(db)
    local = _utc_naive_to_local(b.planned_date, tz) if b.planned_date else None
    if local:
        q["performed_date"] = local.date().isoformat()
    parts: list[str] = [f"бронь #{b.id}"]
    if b.kind == BookingKind.VISIT:
        mode = str(details.get("visit_kit_mode") or "").strip().upper()
        if mode == "ORDER":
            q["scope"] = WorkScope.CUSTOM_ORDER.value
            q["kind"] = WorkKind.KIT.value
            qb = str(details.get("visit_order_blanks_qty") or "").strip()
            if qb:
                parts.append(f"заказ заготовок: {qb} шт")
            qd = str(details.get("visit_order_blanks_desc") or "").strip()
            if qd:
                parts.append(qd[:400])
        elif mode == "OWN" and _booking_details_flag(details.get("visit_own_need_correction")):
            q["scope"] = WorkScope.CUSTOM_ORDER.value
            q["kind"] = WorkKind.KIT_CORRECTION.value
            for key in (
                "corr_trim_qty",
                "corr_hourly_hours",
                "corr_kit_description",
                "corr_kit_blanks_count",
                "corr_wash",
                "corr_steam",
                "corr_circle",
            ):
                val = details.get(key)
                if val is None or str(val).strip() == "":
                    continue
                if str(key).startswith("corr_") and str(val).strip() in ("0", "0.0"):
                    continue
                q[key] = str(val)
    elif b.kind == BookingKind.PRODUCT_SALE:
        pk = (b.planned_product_kind or "").strip().upper()
        if pk == "KIT" and str(details.get("sale_kit_mode") or "").strip().upper() == "ORDER":
            q["scope"] = WorkScope.CUSTOM_ORDER.value
            q["kind"] = WorkKind.KIT.value
            qb = str(details.get("sale_order_blanks_qty") or "").strip()
            if qb:
                parts.append(f"заказ заготовок: {qb} шт")
            qd = str(details.get("sale_order_blanks_desc") or "").strip()
            if qd:
                parts.append(qd[:400])
        elif pk == "RUBBER" and str(details.get("sale_rubber_mode") or "").strip().upper() == "ORDER":
            q["scope"] = WorkScope.CUSTOM_ORDER.value
            q["kind"] = WorkKind.RUBBER.value
            rt = str(details.get("sale_rubber_type") or "").strip()
            if rt:
                q["rubber_type"] = rt
            aq = str(details.get("sale_rubber_attach_qty") or "").strip()
            if aq:
                q["rubber_attach_qty"] = aq
            bq = str(details.get("sale_rubber_braids_qty") or "").strip()
            if bq:
                q["rubber_braids_qty"] = bq
    hint = _amount_hint_from_booking(b)
    if hint:
        q["amount_from_client"] = hint
    comment = "; ".join(parts)
    if b.comment:
        comment = f"{comment}\n{b.comment}" if comment else str(b.comment)
    if comment:
        q["comment"] = comment[:900]
    return q


# Поля комплекта/коррекции брони визита — только для услуг с блоком комплекта (иначе из скрытой формы уезжали дефолты).
_LINE_SERVICE_KIT_FP_RE = re.compile(
    r"^line_(\d+)_(kit_mode|stock_kit_id|stock_kit_pieces|stock_use_entire|stock_breakdown_json|stock_kit_lines_json|"
    r"order_blanks_qty|order_blanks_desc|own_need_correction|corr_wash|corr_trim_qty|corr_hourly_hours|"
    r"corr_kit_description|corr_kit_blanks_count)$"
)


def _expand_line_service_kits_to_fp(fp: dict[str, str]) -> None:
    raw = fp.pop("line_service_kits", None)
    if not raw:
        return
    try:
        kits = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return
    if not isinstance(kits, dict):
        return
    for idx, kit in kits.items():
        if not isinstance(kit, dict):
            continue
        for field, val in kit.items():
            if str(field).startswith("_"):
                continue
            if val is None:
                continue
            sv = str(val).strip()
            if not sv:
                continue
            fp[f"line_{idx}_{field}"] = sv


def _collect_line_service_kits_from_fp(fp: dict[str, str]) -> dict[str, dict[str, str]]:
    acc: dict[str, dict[str, str]] = {}
    for key, val in fp.items():
        m = _LINE_SERVICE_KIT_FP_RE.match(str(key))
        if not m:
            continue
        idx, field = m.group(1), m.group(2)
        if field in ("stock_use_entire", "own_need_correction", "corr_wash"):
            acc.setdefault(idx, {})[field] = "1" if parse_bool(val) else ""
        else:
            sv = str(val or "").strip()
            if not sv:
                continue
            acc.setdefault(idx, {})[field] = sv
    return acc


def _strip_line_service_kits_without_kit_service(
    db: Session, fp: dict[str, str], kits: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    lines_raw = str(fp.get("booking_service_lines_json") or "").strip()
    if not lines_raw:
        return {}
    try:
        lines = json.loads(lines_raw)
    except Exception:
        return {}
    if not isinstance(lines, list):
        return {}
    out: dict[str, dict[str, str]] = {}
    for i, line in enumerate(lines):
        if i == 0 or not isinstance(line, dict):
            continue
        idx = str(i)
        try:
            sid = int(line.get("service_id") or 0)
        except Exception:
            sid = 0
        if sid <= 0:
            continue
        svc = db.get(Service, sid)
        if svc is None or not service_requires_kit_block(svc):
            continue
        if idx in kits:
            out[idx] = kits[idx]
    return out


def _visit_kit_mode_is_in_stock(fp: dict[str, str]) -> bool:
    return str(fp.get("visit_kit_mode") or "").strip().upper() == "IN_STOCK"


def _validate_visit_stock_kit_for_reserve(db: Session, fp: dict[str, str]) -> str | None:
    if str(fp.get("kind") or "").strip() != BookingKind.VISIT.value:
        return None
    if not _visit_kit_mode_is_in_stock(fp):
        return None
    try:
        sid = parse_int(str(fp.get("service_id") or "").strip(), min=1, field_name="service_id")
    except ValueError:
        return None
    svc = db.get(Service, sid)
    if svc is None or not service_requires_kit_block(svc):
        return None
    raw_lines = str(fp.get("visit_stock_kit_lines_json") or "").strip()
    if raw_lines:
        try:
            arr = json.loads(raw_lines)
            if isinstance(arr, list) and any(
                isinstance(x, dict) and int(x.get("kit_id") or 0) > 0 for x in arr
            ):
                return None
        except Exception:
            pass
    kid = str(fp.get("visit_stock_kit_id") or "").strip()
    if not kid.isdigit():
        return "Выберите комплект из наличия для первой услуги."
    return None


def _validate_line_service_kits_for_reserve(db: Session, fp: dict[str, str]) -> str | None:
    line_kits = _strip_line_service_kits_without_kit_service(
        db, fp, _collect_line_service_kits_from_fp(fp)
    )
    for idx, kit_data in sorted(line_kits.items(), key=lambda x: int(x[0])):
        if str(kit_data.get("kit_mode") or "").strip().upper() != "IN_STOCK":
            continue
        raw_lines = str(kit_data.get("stock_kit_lines_json") or "").strip()
        if raw_lines:
            try:
                arr = json.loads(raw_lines)
                if isinstance(arr, list) and any(
                    isinstance(x, dict) and int(x.get("kit_id") or 0) > 0 for x in arr
                ):
                    continue
            except Exception:
                pass
        kid = str(kit_data.get("stock_kit_id") or "").strip()
        if not kid.isdigit():
            return f"Выберите комплект из наличия для услуги {int(idx) + 1}."
    return None


def _planned_service_kit_map(
    db: Session, details: dict[str, Any], booking_kit_reserves: list[KitReserve]
) -> dict[int, Kit]:
    out: dict[int, Kit] = {}
    if str(details.get("visit_kit_mode") or "").strip().upper() == "IN_STOCK":
        k = _visit_stock_kit_from_details(db, details)
        if k is None:
            for r in booking_kit_reserves:
                if r.kit is not None:
                    k = r.kit
                    break
        if k is not None:
            out[0] = k
    kits = details.get("line_service_kits") or {}
    if isinstance(kits, dict):
        for idx_s, row in kits.items():
            if not isinstance(row, dict):
                continue
            if str(row.get("kit_mode") or "").strip().upper() != "IN_STOCK":
                continue
            raw = str(row.get("stock_kit_id") or "").strip()
            if not raw.isdigit():
                continue
            kit = db.get(Kit, int(raw))
            if kit is not None:
                try:
                    out[int(idx_s)] = kit
                except ValueError:
                    pass
    return out


def _line_service_kits_prefill_for_form(db: Session, fp: dict[str, str]) -> dict[str, Any]:
    kits = _collect_line_service_kits_from_fp(fp)
    out: dict[str, Any] = {}
    for idx, kit in kits.items():
        row = dict(kit)
        raw_id = str(row.get("stock_kit_id") or "").strip()
        if raw_id.isdigit():
            k = db.get(Kit, int(raw_id))
            if k is not None:
                row["_kit_initial"] = {
                    "id": int(k.id),
                    "sku": str(k.sku or ""),
                    "title": str(k.title or ""),
                    "pieces_available": int(k.pieces_available or 0),
                }
        out[str(idx)] = row
    return out


_BOOKING_VISIT_KIT_DETAIL_KEYS: frozenset[str] = frozenset(
    (
        "visit_kit_mode",
        "visit_stock_kit_id",
        "visit_stock_kit_pieces",
        "visit_stock_kit_lines_json",
        "visit_stock_breakdown_json",
        "visit_stock_use_entire",
        "visit_own_need_correction",
        "visit_own_need_extra_blanks",
        "visit_extra_blanks_mode",
        "visit_extra_stock_kit_id",
        "visit_extra_stock_kit_lines_json",
        "visit_extra_stock_kit_pieces",
        "visit_extra_stock_breakdown_json",
        "visit_extra_stock_use_entire",
        "visit_order_blanks_qty",
        "visit_order_blanks_desc",
        "visit_order_use_masters",
        "visit_order_master_ids",
        "visit_extra_order_blanks_qty",
        "visit_extra_order_blanks_desc",
        "corr_trim_qty",
        "corr_hourly_hours",
        "corr_kit_description",
        "corr_kit_blanks_count",
        "corr_wash",
        "corr_steam",
        "corr_circle",
    )
)


def _booking_details_from_form(db: Session, fp: dict[str, str]) -> dict[str, object]:
    kind_raw = str(fp.get("kind") or "").strip() or BookingKind.VISIT.value
    product_kind = str(fp.get("product_kind") or "").strip()
    checkbox_keys = (
        "corr_wash",
        "corr_steam",
        "corr_circle",
        "visit_own_need_correction",
        "visit_own_need_extra_blanks",
        "visit_stock_use_entire",
        "visit_extra_stock_use_entire",
        "visit_order_use_masters",
        "sale_stock_use_entire",
    )
    d: dict[str, object] = {}
    keys: tuple[str, ...]
    calc_keys = ("calc_product_min", "calc_product_max", "calc_service_min", "calc_service_max")
    if kind_raw == BookingKind.VISIT.value:
        keys = (
            "visit_custom_duration_on",
            "visit_custom_duration_h",
            "visit_custom_duration_m",
            "visit_kit_mode",
            "visit_stock_kit_id",
            "visit_stock_kit_pieces",
            "visit_stock_kit_lines_json",
            "visit_stock_breakdown_json",
            "visit_stock_use_entire",
            "visit_own_need_correction",
            "visit_own_need_extra_blanks",
            "visit_extra_blanks_mode",
            "visit_extra_stock_kit_id",
            "visit_extra_stock_kit_pieces",
            "visit_extra_stock_breakdown_json",
            "visit_extra_stock_use_entire",
            "visit_order_blanks_qty",
            "visit_order_blanks_desc",
            "visit_order_use_masters",
            "visit_order_master_ids",
            "visit_extra_order_blanks_qty",
            "visit_extra_order_blanks_desc",
        ) + calc_keys
        if str(fp.get("visit_kit_mode") or "").strip().upper() == "OWN":
            keys = keys + ("corr_wash",)
        if parse_bool(fp.get("visit_own_need_correction")):
            keys = keys + (
                "corr_trim_qty",
                "corr_hourly_hours",
                "corr_kit_description",
                "corr_kit_blanks_count",
                "corr_steam",
                "corr_circle",
            )
    elif kind_raw == BookingKind.CONSULTATION.value:
        keys = ("consultation_types_json", "other_text", "comment")
    else:
        keys = ("product_kind",) + calc_keys
        if product_kind == "KIT":
            keys = keys + (
                "sale_kit_mode",
                "sale_stock_kit_id",
                "sale_stock_kit_lines_json",
                "sale_stock_kit_pieces",
                "sale_stock_breakdown_json",
                "sale_stock_use_entire",
                "sale_kit_order_master_ids",
                "sale_order_blanks_qty",
                "sale_order_blanks_desc",
            )
        elif product_kind == "RUBBER":
            keys = keys + (
                "sale_rubber_mode",
                "sale_rubber_order_master_id",
                "sale_rubber_type",
                "sale_rubber_attach_qty",
                "sale_rubber_braids_qty",
                "sale_rubber_desc",
            )
        else:
            keys = keys + ("sale_rubber_desc",)

    for key in keys:
        if key not in fp:
            continue
        v = fp.get(key)
        if v is None:
            continue
        if key in checkbox_keys:
            d[key] = "1" if parse_bool(v) else ""
        else:
            sv = str(v).strip()
            if sv == "":
                continue
            if key.startswith("corr_") and sv in ("0", "0.0"):
                continue
            d[key] = sv

    if kind_raw == BookingKind.VISIT.value:
        # Длительность из строк услуг (новая форма) → minutes в details_json.
        lines_raw = str(fp.get("booking_service_lines_json") or "").strip()
        if lines_raw:
            try:
                parsed_lines = json.loads(lines_raw)
                if isinstance(parsed_lines, list) and len(parsed_lines) == 1 and isinstance(parsed_lines[0], dict):
                    dur_line = _parse_line_duration_minutes(parsed_lines[0].get("duration_minutes"))
                    if dur_line > 0:
                        fp["visit_custom_duration_on"] = "1"
                        fp["visit_custom_duration_h"] = str(dur_line // 60)
                        fp["visit_custom_duration_m"] = str(dur_line % 60)
            except Exception:
                pass
        # Нормализуем кастомную длительность → minutes в details_json (и сохраняем H/M для UI).
        if parse_bool(fp.get("visit_custom_duration_on")):
            raw_h = str(fp.get("visit_custom_duration_h") or "").strip()
            raw_m = str(fp.get("visit_custom_duration_m") or "").strip()
            try:
                hh = int(parse_float(raw_h or "0", min=0.0, field_name="visit_custom_duration_h"))
            except Exception:
                hh = 0
            try:
                mm = int(parse_float(raw_m or "0", min=0.0, field_name="visit_custom_duration_m"))
            except Exception:
                mm = 0
            if mm < 0:
                mm = 0
            if mm > 59:
                mm = 59
            total = int(hh) * 60 + int(mm)
            if total > 0:
                d["visit_custom_duration_minutes"] = int(total)

        strip_visit_kit_keys = True
        try:
            sid = parse_int(str(fp.get("service_id") or "").strip(), min=1, field_name="service_id")
        except ValueError:
            sid = 0
        if sid > 0:
            svc = db.get(Service, sid)
            if svc is not None:
                strip_visit_kit_keys = not service_requires_kit_block(svc)
        if strip_visit_kit_keys:
            for k in _BOOKING_VISIT_KIT_DETAIL_KEYS:
                d.pop(k, None)

        line_kits = _strip_line_service_kits_without_kit_service(
            db, fp, _collect_line_service_kits_from_fp(fp)
        )
        if line_kits:
            d["line_service_kits"] = line_kits
        else:
            d.pop("line_service_kits", None)

    return {k: v for k, v in d.items() if not (isinstance(v, str) and str(v).strip() == "")}


def _sync_booking_staff_rows_for_sale(db: Session, *, booking_id: int, fp: dict[str, str], form_raw) -> None:
    for r in list(
        db.scalars(
            select(BookingStaff).where(
                BookingStaff.booking_id == booking_id,
                BookingStaff.kind.in_([BookingStaffKind.SALE_KIT_ORDER, BookingStaffKind.SALE_RUBBER_ORDER]),
            )
        ).all()
    ):
        db.delete(r)
    db.flush()

    if (fp.get("kind") or "") == BookingKind.VISIT.value and (fp.get("visit_kit_mode") or "") == "ORDER":
        if parse_bool(fp.get("visit_order_use_masters")):
            ids: list[int] = []
            for v in form_raw.getlist("visit_order_master_on"):
                try:
                    ids.append(parse_int(v, min=1, field_name="visit_order_master_on"))
                except Exception:
                    pass
            ids = sorted(set([i for i in ids if i > 0]))
            for uid in ids:
                if db.get(User, uid) is None:
                    continue
                db.add(BookingStaff(booking_id=booking_id, user_id=uid, kind=BookingStaffKind.SALE_KIT_ORDER))
            if ids:
                fp["visit_order_master_ids"] = ",".join([str(i) for i in ids])
                fp["visit_order_use_masters"] = "1"
            else:
                fp.pop("visit_order_master_ids", None)
                fp.pop("visit_order_use_masters", None)
        else:
            fp.pop("visit_order_master_ids", None)
            fp.pop("visit_order_use_masters", None)
        db.flush()
        return

    if (fp.get("product_kind") or "") == "KIT" and (fp.get("sale_kit_mode") or "") == "ORDER":
        ids: list[int] = []
        for v in form_raw.getlist("sale_kit_order_master_on"):
            try:
                ids.append(parse_int(v, min=1, field_name="sale_kit_order_master_on"))
            except Exception:
                pass
        ids = sorted(set([i for i in ids if i > 0]))
        for uid in ids:
            if db.get(User, uid) is None:
                continue
            db.add(BookingStaff(booking_id=booking_id, user_id=uid, kind=BookingStaffKind.SALE_KIT_ORDER))
        fp["sale_kit_order_master_ids"] = ",".join([str(i) for i in ids])

    if (fp.get("product_kind") or "") == "RUBBER" and (fp.get("sale_rubber_mode") or "") == "ORDER":
        raw = str(fp.get("sale_rubber_order_master_id") or "").strip()
        try:
            uid = parse_int(raw, min=1, field_name="sale_rubber_order_master_id")
        except ValueError:
            uid = 0
        if uid > 0 and db.get(User, uid) is not None:
            db.add(BookingStaff(booking_id=booking_id, user_id=uid, kind=BookingStaffKind.SALE_RUBBER_ORDER))
    db.flush()


def release_booking_kit_reserves(db: Session, *, booking_id: int, changed_by_user_id: int | None) -> None:
    """Вернуть заготовки на склад и удалить строки резерва, привязанные к брони."""
    rows = list(
        db.scalars(
            select(KitReserve).where(KitReserve.booking_id == int(booking_id)).order_by(KitReserve.id.asc())
        ).all()
    )
    for r in rows:
        kit = db.get(Kit, r.kit_id)
        if kit is None:
            db.delete(r)
            continue
        before = SimpleNamespace(pieces_available=kit.pieces_available)
        return_reserve_row_to_stock(db, kit, r)
        kit.updated_at = utcnow_naive()
        if changed_by_user_id is not None:
            kit.updated_by_user_id = changed_by_user_id
        db.delete(r)
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=changed_by_user_id,
            changes=diff_fields(before, kit, ("pieces_available",)),
        )


def _apply_booking_auto_reserves(
    db: Session, *, booking_id: int, booking_client_id: int, fp: dict[str, str], changed_by_user_id: int
) -> None:
    def _reserve_kit(
        kit_id_raw: str | None,
        pieces_field: str | None,
        *,
        use_entire_field: str | None = None,
        breakdown_json_field: str | None = None,
        reserve_fp: dict[str, str] | None = None,
    ) -> None:
        fp_src = reserve_fp if reserve_fp is not None else fp
        if not kit_id_raw:
            return
        kit_id_raw = str(kit_id_raw).strip()
        try:
            kit_id = parse_int(kit_id_raw, min=1, field_name="kit_id")
        except ValueError:
            return
        kit = db.get(Kit, kit_id)
        if not kit:
            return
        if kit_inventory_is_keyed(db, kit.id):
            sm = blank_stock_qty_map(db, kit.id)
            max_by_key = max_take_by_key_for_client(
                db, kit=kit, client_id=booking_client_id, stock_map=sm
            )
            if sum(max_by_key.values()) <= 0:
                return
            use_entire = bool(use_entire_field and parse_bool(fp_src.get(use_entire_field)))
            raw_j = (str(fp_src.get(breakdown_json_field) or "").strip() if breakdown_json_field else "")
            usage_by_key: dict[str, int] | None = None
            if raw_j:
                try:
                    d = json.loads(raw_j)
                    if isinstance(d, dict):
                        usage_by_key = {str(k): int(v) for k, v in d.items() if int(v) > 0}
                except Exception:
                    usage_by_key = None
            pq = "" if use_entire else (str(fp_src.get(pieces_field) or "").strip() if pieces_field else "")
            try:
                blanks_used = parse_int(pq, min=0, field_name="reserve_pieces") if pq else 0
            except ValueError:
                blanks_used = 0
            try:
                bd = build_usage_breakdown_keyed(
                    use_entire=use_entire,
                    blanks_used=blanks_used,
                    usage_by_key=usage_by_key,
                    max_by_key=max_by_key,
                )
            except ValueError:
                return
            slot_rows = sum(1 for _k, n in bd.items() if int(n) > 0)
            if slot_rows <= 0:
                return
            if kit_reserve_slots_used(db, kit.id) + slot_rows > get_kit_max_reserves_per_kit(db):
                return
            before = SimpleNamespace(pieces_available=kit.pieces_available)
            for kk, n in bd.items():
                qn = int(n)
                if qn <= 0:
                    continue
                consume_blank_stock_for_reserve(
                    db, kit, kit_key=kk, qty=qn, sync_after=False
                )
                db.add(
                    KitReserve(
                        kit_id=kit.id,
                        kit_key=str(kk)[:80],
                        pieces_reserved=qn,
                        reserved_at=utcnow_naive(),
                        reserved_by_user_id=changed_by_user_id,
                        reserved_for_client_id=booking_client_id,
                        reserved_for_user_id=None,
                        booking_id=int(booking_id),
                    )
                )
            sync_kit_pieces_available_from_blank_lines(db, kit)
            kit.updated_at = utcnow_naive()
            kit.updated_by_user_id = changed_by_user_id
            write_audit_rows(
                db,
                log_model=KitAuditLog,
                entity_field="kit_id",
                entity_id=kit.id,
                changed_by_user_id=changed_by_user_id,
                changes=diff_fields(before, kit, ("pieces_available",)),
            )
            return

        avail = int(kit.pieces_available or 0)
        if avail <= 0:
            return
        if kit_reserve_slots_used(db, kit.id) >= get_kit_max_reserves_per_kit(db):
            return
        use_entire = bool(use_entire_field and parse_bool(fp_src.get(use_entire_field)))
        pq = "" if use_entire else (str(fp_src.get(pieces_field) or "").strip() if pieces_field else "")
        try:
            qty = parse_int(pq, min=1, field_name="reserve_pieces") if pq else avail
        except ValueError:
            qty = avail
        qty = max(1, min(qty, avail))
        before = SimpleNamespace(pieces_available=kit.pieces_available)
        kit.pieces_available = avail - qty
        kit.updated_at = utcnow_naive()
        kit.updated_by_user_id = changed_by_user_id
        db.add(
            KitReserve(
                kit_id=kit.id,
                pieces_reserved=qty,
                reserved_at=utcnow_naive(),
                reserved_by_user_id=changed_by_user_id,
                reserved_for_client_id=booking_client_id,
                reserved_for_user_id=None,
                booking_id=int(booking_id),
            )
        )
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=changed_by_user_id,
            changes=diff_fields(before, kit, ("pieces_available",)),
        )

    def _reserve_stock_lines(
        src_fp: dict[str, str],
        *,
        pieces_field: str,
        use_entire_field: str,
        breakdown_field: str,
    ) -> None:
        from app.kit_inlay_visit import _parse_stock_kit_lines_from_form

        class _A:
            def get(self, name: str, default: Any = None) -> Any:
                return src_fp.get(name, default)

            def getlist(self, name: str) -> list[Any]:
                v = src_fp.get(name)
                return [v] if v is not None else []

        def g(name: str, default: str = "") -> str:
            if name == "stock_kit_lines_json":
                return str(
                    src_fp.get("visit_stock_kit_lines_json") or src_fp.get("stock_kit_lines_json") or default
                )
            if name == "stock_kit_id":
                return str(src_fp.get("visit_stock_kit_id") or src_fp.get("stock_kit_id") or default)
            if name == "stock_use_entire":
                return str(src_fp.get("visit_stock_use_entire") or src_fp.get("stock_use_entire") or default)
            if name == "stock_blanks_used":
                return str(
                    src_fp.get("visit_stock_kit_pieces") or src_fp.get("stock_kit_pieces") or default
                )
            if name == "stock_breakdown_json":
                return str(
                    src_fp.get("visit_stock_breakdown_json") or src_fp.get("stock_breakdown_json") or default
                )
            return str(src_fp.get(name, default) or "")

        def g_int(name: str, default: int = 0) -> int:
            raw = g(name, "").strip()
            if not raw:
                return default
            try:
                return int(parse_float(raw, field_name=name))
            except ValueError:
                return default

        def g_bool(name: str) -> bool:
            return parse_bool(src_fp.get(name))

        try:
            lines = _parse_stock_kit_lines_from_form(_A(), g, g_int, g_bool)
        except ValueError:
            lines = []
        if not lines and str(src_fp.get("visit_stock_kit_id") or src_fp.get("stock_kit_id") or "").strip():
            _reserve_kit(
                src_fp.get("visit_stock_kit_id") or src_fp.get("stock_kit_id"),
                pieces_field,
                use_entire_field=use_entire_field,
                breakdown_json_field=breakdown_field,
                reserve_fp=src_fp,
            )
            return
        for line in lines:
            line_fp = {
                pieces_field: str(line.blanks_used),
                use_entire_field: "1" if line.use_entire else "",
                breakdown_field: json.dumps(line.usage_by_key, ensure_ascii=False) if line.usage_by_key else "",
            }
            _reserve_kit(
                str(line.kit_id),
                pieces_field,
                use_entire_field=use_entire_field,
                breakdown_json_field=breakdown_field,
                reserve_fp=line_fp,
            )

    if _visit_kit_mode_is_in_stock(fp):
        _reserve_stock_lines(
            fp,
            pieces_field="visit_stock_kit_pieces",
            use_entire_field="visit_stock_use_entire",
            breakdown_field="visit_stock_breakdown_json",
        )
    line_kits = _strip_line_service_kits_without_kit_service(
        db, fp, _collect_line_service_kits_from_fp(fp)
    )
    for _idx, kit_data in line_kits.items():
        if str(kit_data.get("kit_mode") or "").strip().upper() != "IN_STOCK":
            continue
        line_src = dict(kit_data)
        line_src["visit_stock_kit_id"] = line_src.get("stock_kit_id")
        line_src["visit_stock_kit_pieces"] = line_src.get("stock_kit_pieces")
        line_src["visit_stock_use_entire"] = line_src.get("stock_use_entire")
        line_src["visit_stock_breakdown_json"] = line_src.get("stock_breakdown_json")
        line_src["visit_stock_kit_lines_json"] = line_src.get("stock_kit_lines_json")
        _reserve_stock_lines(
            line_src,
            pieces_field="visit_stock_kit_pieces",
            use_entire_field="visit_stock_use_entire",
            breakdown_field="visit_stock_breakdown_json",
        )
    if (fp.get("visit_kit_mode") or "") == "OWN" and (fp.get("visit_own_need_extra_blanks") or ""):
        if (fp.get("visit_extra_blanks_mode") or "") == "IN_STOCK":
            extra_src = {
                "visit_stock_kit_lines_json": fp.get("visit_extra_stock_kit_lines_json"),
                "visit_stock_kit_id": fp.get("visit_extra_stock_kit_id"),
                "visit_stock_kit_pieces": fp.get("visit_extra_stock_kit_pieces"),
                "visit_stock_use_entire": fp.get("visit_extra_stock_use_entire"),
                "visit_stock_breakdown_json": fp.get("visit_extra_stock_breakdown_json"),
            }
            _reserve_stock_lines(
                extra_src,
                pieces_field="visit_extra_stock_kit_pieces",
                use_entire_field="visit_extra_stock_use_entire",
                breakdown_field="visit_extra_stock_breakdown_json",
            )
    if (fp.get("product_kind") or "") == "KIT" and (fp.get("sale_kit_mode") or "") == "IN_STOCK":
        sale_src = {
            "visit_stock_kit_lines_json": fp.get("sale_stock_kit_lines_json"),
            "visit_stock_kit_id": fp.get("sale_stock_kit_id"),
            "visit_stock_kit_pieces": fp.get("sale_stock_kit_pieces"),
            "visit_stock_use_entire": fp.get("sale_stock_use_entire"),
            "visit_stock_breakdown_json": fp.get("sale_stock_breakdown_json"),
        }
        _reserve_stock_lines(
            sale_src,
            pieces_field="sale_stock_kit_pieces",
            use_entire_field="sale_stock_use_entire",
            breakdown_field="sale_stock_breakdown_json",
        )


def _masters_for_visit_form(db: Session) -> list[User]:
    return list(db.scalars(select_users_with_role(UserRole.MASTER).order_by(User.display_name.asc(), User.username.asc())).all())


@router.get("/new", response_class=HTMLResponse)
def admin_booking_new_get(
    request: Request,
    client_id: int | None = None,
    consultation_id: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    consultation: Consultation | None = None
    consultation_comment: str | None = None
    if consultation_id:
        consultation = db.get(Consultation, consultation_id)
        if consultation is None:
            raise HTTPException(status_code=404, detail="Консультация не найдена")
        if not can_create_booking_from_consultation(db, consultation):
            raise HTTPException(status_code=400, detail="Для этой консультации уже есть активная или выполненная бронь")
        client_id = consultation.client_id
        consultation_comment = consultation.comment

    selected_client = db.get(Client, client_id) if client_id else None
    masters = _masters_for_visit_form(db)
    service_catalog = list_master_visit_services_catalog(db)
    staff_users = list(db.scalars(select_users_with_any_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)).all())
    fp: dict[str, str] = {"planned_date": "", "planned_time": ""}
    allow = {
        "kind",
        "service_id",
        "product_kind",
        "visit_kit_mode",
        "visit_stock_kit_id",
        "visit_stock_kit_pieces",
        "visit_stock_breakdown_json",
        "visit_stock_use_entire",
        "visit_own_need_correction",
        "visit_own_need_extra_blanks",
        "visit_extra_blanks_mode",
        "visit_extra_stock_kit_id",
        "visit_extra_stock_kit_lines_json",
        "visit_extra_stock_kit_pieces",
        "visit_extra_stock_breakdown_json",
        "visit_extra_stock_use_entire",
        "visit_order_blanks_qty",
        "visit_order_blanks_desc",
        "visit_order_use_masters",
        "visit_order_master_ids",
        "visit_extra_order_blanks_qty",
        "visit_extra_order_blanks_desc",
        "corr_trim_qty",
        "corr_hourly_hours",
        "corr_kit_description",
        "corr_kit_blanks_count",
        "corr_wash",
        "corr_steam",
        "corr_circle",
        "sale_kit_mode",
        "sale_stock_kit_id",
        "sale_stock_kit_pieces",
        "sale_stock_breakdown_json",
        "sale_stock_use_entire",
        "sale_kit_order_master_ids",
        "sale_order_blanks_qty",
        "sale_order_blanks_desc",
        "sale_rubber_mode",
        "sale_rubber_order_master_id",
        "sale_rubber_type",
        "sale_rubber_attach_qty",
        "sale_rubber_braids_qty",
        "sale_rubber_desc",
        "quoted_price_text",
        "deposit_amount",
        "comment",
        "calc_product_min",
        "calc_product_max",
        "calc_service_min",
        "calc_service_max",
        "planned_date",
        "planned_time",
        "planned_service_ids",
        "booking_service_lines_json",
        "booking_masters_mode",
    }
    for k, v in request.query_params.items():
        if k in allow and v is not None:
            fp[k] = str(v)
    if consultation:
        _prefill_booking_fp_from_consultation(db, consultation, fp)
    return templates.TemplateResponse(
        "admin_booking_form.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            is_new=True,
            booking=None,
            selected_client=selected_client,
            masters=masters,
            staff_users=staff_users,
            after_reserve=str(request.url),
            service_catalog=service_catalog,
            kind_options=_BOOKING_KIND_OPTIONS,
            product_kind_options=[k.value for k in ProductSaleKind],
            rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
            fp=fp,
            booking_master_on_ids=[],
            visit_stock_kit_initial=_visit_stock_kit_initial_for_form(db, fp),
            line_service_kits_prefill=_line_service_kits_prefill_for_form(db, fp),
            consultation_id=consultation.id if consultation else None,
            consultation_comment=consultation_comment,
            **_booking_form_consultation_template_ctx(db, fp),
        ),
    )


@router.post("/new")
@legacy_bookings_admin_router.post("/new")
async def admin_booking_new_post(  # noqa: C901
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form_raw = await request.form()
    fp = {k: form_raw.get(k) for k in form_raw.keys() if not isinstance(form_raw.get(k), UploadFile)}
    visit_order_master_ids: list[int] = []
    for v in form_raw.getlist("visit_order_master_on"):
        try:
            visit_order_master_ids.append(parse_int(v, min=1, field_name="visit_order_master_on"))
        except Exception:
            pass
    visit_order_master_ids = sorted(set([i for i in visit_order_master_ids if i > 0]))
    if visit_order_master_ids:
        fp["visit_order_master_ids"] = ",".join([str(i) for i in visit_order_master_ids])
    else:
        fp.pop("visit_order_master_ids", None)
    client_id_raw = str(fp.get("client_id") or "").strip()
    kind_raw = str(fp.get("kind") or "").strip() or BookingKind.VISIT.value
    quoted_price_text = strip_or_none(str(fp.get("quoted_price_text") or ""), 120)
    deposit_raw = str(fp.get("deposit_amount") or "").strip()
    comment = strip_or_none(str(fp.get("comment") or "")) or None
    photo_1_url: str | None = None
    photo_2_url: str | None = None
    photo_3_url: str | None = None

    err: str | None = None
    client: Client | None = None
    planned_date: datetime | None = None
    deposit_amount: int | None = None
    planned_service_id: int | None = None
    planned_product_kind: str | None = None
    planned_service_ids: list[int] = []
    planned_service_lines: list[dict[str, Any]] = []
    on_ids: list[int] = []

    try:
        client_id = parse_int(client_id_raw, min=1, field_name="client_id")
    except ValueError:
        client_id = 0
    if client_id <= 0:
        err = "Выберите клиента."
    else:
        client = db.get(Client, client_id)
        if client is None:
            err = "Клиент не найден."

    tz_name: str | None = None
    if not err:
        try:
            tz_name = get_display_timezone(db)
            planned_date = _parse_planned_booking_datetime(fp, tz_name)
        except Exception:
            err = "Укажите дату и время брони."

    if not err:
        if kind_raw not in _BOOKING_KIND_OPTIONS:
            err = "Некорректный тип брони."

    if not err and deposit_raw:
        try:
            dep = parse_float(deposit_raw, min=0.0, field_name="deposit_amount")
            deposit_amount = int(dep)
        except Exception:
            err = "Предоплата должна быть числом."

    if not err and kind_raw == BookingKind.VISIT.value:
        svc_err, planned_service_ids, planned_service_id, planned_service_lines, on_ids = _parse_booking_visit_lines_and_masters(
            db, form_raw, fp
        )
        if svc_err:
            err = svc_err
        else:
            _apply_parsed_visit_lines_to_fp(fp, planned_service_lines)
            _logger.info(
                "booking create: %s service line(s), service_ids=%s, durations=%s, masters=%s",
                len(planned_service_lines),
                [int(x["service_id"]) for x in planned_service_lines],
                [_parse_line_duration_minutes(x.get("duration_minutes")) for x in planned_service_lines],
                on_ids,
            )
        if not err and (fp.get("visit_kit_mode") or "") == "ORDER" and parse_bool(fp.get("visit_order_use_masters")):
            if not visit_order_master_ids:
                err = "Выберите хотя бы одного мастера для заказа комплекта."
        if not err:
            kit_err = _validate_visit_stock_kit_for_reserve(db, fp)
            if kit_err:
                err = kit_err
        if not err:
            line_kit_err = _validate_line_service_kits_for_reserve(db, fp)
            if line_kit_err:
                err = line_kit_err

    if not err and kind_raw == BookingKind.PRODUCT_SALE.value:
        pk = str(fp.get("product_kind") or "").strip()
        if pk not in [k.value for k in ProductSaleKind]:
            err = "Выберите тип продажи."
        else:
            planned_product_kind = pk
            if pk == ProductSaleKind.KIT.value and (fp.get("sale_kit_mode") or "") == "ORDER":
                if len(form_raw.getlist("sale_kit_order_master_on")) == 0:
                    err = "Выберите хотя бы одного мастера для заказа комплекта."
            if pk == ProductSaleKind.RUBBER.value and (fp.get("sale_rubber_mode") or "") == "ORDER":
                raw_mid = str(fp.get("sale_rubber_order_master_id") or "").strip()
                try:
                    _ = parse_int(raw_mid, min=1, field_name="sale_rubber_order_master_id")
                except ValueError:
                    err = "Выберите мастера для заказа хвоста/резинки."

    if not err and kind_raw == BookingKind.CONSULTATION.value:
        cons_err, types_data, planned_service_ids = _parse_booking_consultation_fields(db, form_raw, fp)
        if cons_err:
            err = cons_err
        else:
            fp["consultation_types_json"] = types_json_dumps(types_data)
            planned_service_id = planned_service_ids[0] if planned_service_ids else None

    masters = _masters_for_visit_form(db)
    service_catalog = list_master_visit_services_catalog(db)
    staff_users = list(db.scalars(select_users_with_any_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)).all())

    if err:
        return templates.TemplateResponse(
            "admin_booking_form.html",
            _ctx(
                request,
                current_user=current_user,
                error=err,
                is_new=True,
                booking=None,
                selected_client=client,
                masters=masters,
                staff_users=staff_users,
                after_reserve=str(request.url),
                service_catalog=service_catalog,
                kind_options=_BOOKING_KIND_OPTIONS,
                product_kind_options=[k.value for k in ProductSaleKind],
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                fp=fp,
                booking_master_on_ids=[],
                consultation_id=int(fp["consultation_id"]) if str(fp.get("consultation_id") or "").isdigit() else None,
                consultation_comment=None,
                **_booking_form_consultation_template_ctx(db, fp),
            ),
            status_code=400,
        )

    assert client is not None and planned_date is not None
    if tz_name is None:
        tz_name = get_display_timezone(db)

    # --- График мастеров (жёсткая server-validation при создании брони) ---
    local_day = _booking_local_day_from_fp(fp, planned_date_utc=planned_date, tz_name=tz_name)
    if kind_raw == BookingKind.VISIT.value:
        dur_override = _booking_custom_duration_override_minutes(fp)
        avail_err = _validate_booking_visit_line_availability(
            db,
            local_day=local_day,
            line_specs=planned_service_lines,
            duration_override_minutes=dur_override,
        )
        if avail_err:
            return templates.TemplateResponse(
                "admin_booking_form.html",
                _ctx(
                    request,
                    current_user=current_user,
                    error=avail_err,
                    is_new=True,
                    booking=None,
                    selected_client=client,
                    masters=masters,
                    staff_users=staff_users,
                    after_reserve=str(request.url),
                    service_catalog=service_catalog,
                    kind_options=_BOOKING_KIND_OPTIONS,
                    product_kind_options=[k.value for k in ProductSaleKind],
                    rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                    fp=fp,
                    booking_master_on_ids=on_ids,
                    consultation_id=int(fp["consultation_id"]) if str(fp.get("consultation_id") or "").isdigit() else None,
                    consultation_comment=None,
                ),
                status_code=400,
            )
    try:
        up1 = get_nonempty_upload(form_raw, "photo_1")
        up2 = get_nonempty_upload(form_raw, "photo_2")
        up3 = get_nonempty_upload(form_raw, "photo_3")
        if up1 is not None:
            photo_1_url = await save_upload_image(up1)
        if up2 is not None:
            photo_2_url = await save_upload_image(up2)
        if up3 is not None:
            photo_3_url = await save_upload_image(up3)
    except ValueError as exc:
        err = str(exc)
        masters = _masters_for_visit_form(db)
        service_catalog = list_master_visit_services_catalog(db)
        staff_users = list(db.scalars(select_users_with_any_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)).all())
        return templates.TemplateResponse(
            "admin_booking_form.html",
            _ctx(
                request,
                current_user=current_user,
                error=err,
                is_new=True,
                booking=None,
                selected_client=client,
                masters=masters,
                staff_users=staff_users,
                after_reserve=str(request.url),
                service_catalog=service_catalog,
                kind_options=_BOOKING_KIND_OPTIONS,
                product_kind_options=[k.value for k in ProductSaleKind],
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                fp=fp,
                booking_master_on_ids=[],
            ),
            status_code=400,
        )

    consultation_id_val: int | None = None
    cons_raw = str(fp.get("consultation_id") or "").strip()
    if cons_raw.isdigit():
        consultation_id_val = int(cons_raw)
        cons_chk = db.get(Consultation, consultation_id_val)
        if cons_chk is None:
            err = "Консультация не найдена."
        elif not can_create_booking_from_consultation(db, cons_chk):
            err = "Для этой консультации уже есть активная или выполненная бронь."
        else:
            if not photo_1_url and (fp.get("prefill_photo_1") or cons_chk.photo_1):
                photo_1_url = str(fp.get("prefill_photo_1") or cons_chk.photo_1)
            if not photo_2_url and (fp.get("prefill_photo_2") or cons_chk.photo_2):
                photo_2_url = str(fp.get("prefill_photo_2") or cons_chk.photo_2)

    if err:
        cons_id_ctx = consultation_id_val
        cons_comment_ctx = None
        if cons_id_ctx:
            c0 = db.get(Consultation, cons_id_ctx)
            cons_comment_ctx = c0.comment if c0 else None
        return templates.TemplateResponse(
            "admin_booking_form.html",
            _ctx(
                request,
                current_user=current_user,
                error=err,
                is_new=True,
                booking=None,
                selected_client=client,
                masters=masters,
                staff_users=staff_users,
                after_reserve=str(request.url),
                service_catalog=service_catalog,
                kind_options=_BOOKING_KIND_OPTIONS,
                product_kind_options=[k.value for k in ProductSaleKind],
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                fp=fp,
                booking_master_on_ids=[],
                consultation_id=cons_id_ctx,
                consultation_comment=cons_comment_ctx,
            ),
            status_code=400,
        )

    booking = Booking(
        created_by_user_id=current_user.id,
        client_id=client.id,
        planned_date=planned_date,
        kind=BookingKind(kind_raw),
        status=BookingStatus.PENDING_CONFIRMATION,
        quoted_price_text=quoted_price_text,
        deposit_amount=deposit_amount,
        photo_1=photo_1_url,
        photo_2=photo_2_url,
        photo_3=photo_3_url,
        comment=comment,
        planned_service_id=planned_service_id,
        planned_product_kind=planned_product_kind,
        consultation_id=consultation_id_val,
        details_json=json.dumps(_booking_details_from_form(db, fp), ensure_ascii=False),
    )
    db.add(booking)
    db.flush()

    if kind_raw == BookingKind.VISIT.value and planned_service_lines:
        _sync_booking_planned_services_with_lines(
            db,
            booking_id=booking.id,
            local_day=local_day,
            tz_name=tz_name,
            line_specs=planned_service_lines,
        )

    if kind_raw == BookingKind.VISIT.value:
        for mid in on_ids:
            if db.get(User, mid) is None:
                continue
            db.add(BookingMaster(booking_id=booking.id, master_id=mid))

    if kind_raw == BookingKind.CONSULTATION.value:
        from app.planned_services_db import sync_booking_planned_services

        sync_booking_planned_services(
            db,
            booking.id,
            planned_service_ids,
            planned_date=planned_date,
        )

    _sync_booking_staff_rows_for_sale(db, booking_id=booking.id, fp=fp, form_raw=form_raw)
    _apply_booking_auto_reserves(
        db,
        booking_id=booking.id,
        booking_client_id=booking.client_id,
        fp=fp,
        changed_by_user_id=current_user.id,
    )
    _refresh_sale_order_master_ids_in_fp(db, booking_id=booking.id, fp=fp)
    booking.details_json = json.dumps(_booking_details_from_form(db, fp), ensure_ascii=False)
    db.commit()
    return RedirectResponse(url=f"/bookings/{booking.id}?msg=created", status_code=303)


@router.get("/{booking_id}", response_class=HTMLResponse)
def admin_booking_detail(
    request: Request,
    booking_id: int,
    msg: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    b = db.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .options(
            selectinload(Booking.client),
            selectinload(Booking.created_by_user),
            selectinload(Booking.updated_by_user),
            selectinload(Booking.cancelled_by_user),
            selectinload(Booking.planned_service).selectinload(Service.subcategory).selectinload(ServiceSubcategory.category),
            selectinload(Booking.masters).selectinload(BookingMaster.master),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.service)
            .selectinload(Service.subcategory)
            .selectinload(ServiceSubcategory.category),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.masters)
            .selectinload(BookingPlannedServiceMaster.master),
            selectinload(Booking.consultation).selectinload(Consultation.created_by_user),
        )
    )
    if b is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")

    linked_consultation = b.consultation
    consultation_for_booking_row = db.scalar(
        select(Consultation)
        .where(Consultation.source_booking_id == booking_id)
        .options(selectinload(Consultation.created_by_user))
        .limit(1)
    )
    need_consultation_for = b.kind == BookingKind.CONSULTATION
    can_create_consultation_for = can_create_consultation_from_booking(db, booking_id)
    consultation_for_create_url = f"/consultations/new?booking_id={booking_id}" if need_consultation_for else ""
    linked_visit_id = db.scalar(select(Visit.id).where(Visit.booking_id == booking_id).limit(1))
    linked_sale_id = db.scalar(select(ProductSale.id).where(ProductSale.booking_id == booking_id).limit(1))
    linked_work_id = db.scalar(select(WorkForInventory.id).where(WorkForInventory.booking_id == booking_id).limit(1))

    booking_kit_reserves = list(
        db.scalars(
            select(KitReserve)
            .where(KitReserve.booking_id == booking_id)
            .options(selectinload(KitReserve.kit))
            .order_by(KitReserve.id.asc())
        ).all()
    )

    sale_staff = list(
        db.scalars(
            select(BookingStaff).where(BookingStaff.booking_id == booking_id).options(selectinload(BookingStaff.user))
        ).all()
    )
    sale_kit_order_users = [
        (r.user.display_name if r.user else f"#{r.user_id}") for r in sale_staff if r.kind == BookingStaffKind.SALE_KIT_ORDER
    ]
    sale_rubber_order_users = [
        (r.user.display_name if r.user else f"#{r.user_id}") for r in sale_staff if r.kind == BookingStaffKind.SALE_RUBBER_ORDER
    ]

    audit_rows = list(
        db.scalars(
            select(BookingAuditLog)
            .options(selectinload(BookingAuditLog.changed_by_user))
            .where(BookingAuditLog.booking_id == booking_id)
            .order_by(BookingAuditLog.changed_at.desc(), BookingAuditLog.id.desc())
            .limit(200)
        ).all()
    )
    display_tz = get_display_timezone(db)
    details: dict[str, Any] = {}
    if b.details_json:
        try:
            raw = json.loads(b.details_json)
            if isinstance(raw, dict):
                details = raw
        except Exception:
            details = {}

    calc_product_min = str(details.get("calc_product_min") or "").strip()
    calc_product_max = str(details.get("calc_product_max") or "").strip()
    calc_service_min = str(details.get("calc_service_min") or "").strip()
    calc_service_max = str(details.get("calc_service_max") or "").strip()
    if (not calc_service_min and not calc_service_max) and b.planned_service:
        lo, hi = None, None
        try:
            svc = b.planned_service
            vals = [(svc.price_junior_from, svc.price_junior_to), (svc.price_middle_from, svc.price_middle_to), (svc.price_senior_from, svc.price_senior_to)]
            lows: list[float] = []
            highs: list[float] = []
            for fr, to in vals:
                if fr is not None:
                    lows.append(float(fr))
                if to is not None:
                    highs.append(float(to))
            if not highs and lows:
                highs = list(lows)
            if not lows and highs:
                lows = list(highs)
            lo = min(lows) if lows else None
            hi = max(highs) if highs else None
        except Exception:
            lo, hi = None, None
        if lo is not None or hi is not None:
            calc_service_min = "" if lo is None else f"{float(lo):.0f}"
            calc_service_max = "" if hi is None else f"{float(hi):.0f}"

    need_work = booking_linked_need_work(b, details)
    need_visit = booking_linked_need_visit(b)
    need_sale = booking_linked_need_sale(b)
    visit_blocked = need_visit and not linked_visit_id and need_work and not linked_work_id
    sale_blocked = need_sale and not linked_sale_id and need_work and not linked_work_id

    work_create_url = ""
    if need_work and not linked_work_id:
        work_q = _booking_work_new_query_params(db, b, details)
        work_create_url = "/sales/work/new?" + urlencode(work_q, quote_via=quote)
    visit_create_url = f"/master/visit/new?booking_id={b.id}" if need_visit and not linked_visit_id else ""
    sale_create_url = f"/sales/products/new?booking_id={b.id}" if need_sale and not linked_sale_id else ""
    booking_can_create_master_records = current_user.role == UserRole.MASTER
    booking_link_master_only_title = (
        "Создание работы и визита доступно только под активной ролью «Мастер». "
        "Выберите роль мастера при входе или попросите мастера создать запись."
    )
    return templates.TemplateResponse(
        "admin_booking_detail.html",
        _ctx(
            request,
            current_user=current_user,
            booking=b,
            kind_label=_booking_kind_label(b.kind.value),
            status_label=_booking_status_label(b.status.value),
            product_kind_label=_product_kind_label(b.planned_product_kind),
            booking_can_create_master_records=booking_can_create_master_records,
            booking_link_master_only_title=booking_link_master_only_title,
            flash_msg=msg,
            linked_visit_id=linked_visit_id,
            linked_sale_id=linked_sale_id,
            linked_work_id=linked_work_id,
            linked_consultation=linked_consultation,
            consultation_for_booking=consultation_for_booking_row,
            need_consultation_for=need_consultation_for,
            can_create_consultation_for=can_create_consultation_for,
            consultation_for_create_url=consultation_for_create_url,
            audit_rows=audit_rows,
            display_tz=display_tz,
            sale_kit_order_users=sale_kit_order_users,
            sale_rubber_order_users=sale_rubber_order_users,
            details=details,
            calc_product_min=calc_product_min,
            calc_product_max=calc_product_max,
            calc_service_min=calc_service_min,
            calc_service_max=calc_service_max,
            need_work=need_work,
            need_visit=need_visit,
            need_sale=need_sale,
            visit_blocked=visit_blocked,
            sale_blocked=sale_blocked,
            work_create_url=work_create_url,
            visit_create_url=visit_create_url,
            sale_create_url=sale_create_url,
            booking_kit_reserves=booking_kit_reserves,
            visit_stock_kit=_visit_stock_kit_from_details(db, details),
            planned_service_kits=_planned_service_kit_map(db, details, booking_kit_reserves),
        ),
    )


@router.get("/{booking_id}/edit", response_class=HTMLResponse)
def admin_booking_edit_get(
    request: Request,
    booking_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    b = db.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .options(
            selectinload(Booking.masters),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.masters),
        )
    )
    if b is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    selected_client = db.get(Client, b.client_id)
    masters = _masters_for_visit_form(db)
    service_catalog = list_master_visit_services_catalog(db)
    staff_users = list(db.scalars(select_users_with_any_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)).all())
    fp, master_ids = _booking_form_prefill_from_db(db, b)
    return templates.TemplateResponse(
        "admin_booking_form.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            is_new=False,
            booking=b,
            selected_client=selected_client,
            masters=masters,
            staff_users=staff_users,
            after_reserve=str(request.url),
            service_catalog=service_catalog,
            kind_options=_BOOKING_KIND_OPTIONS,
            product_kind_options=[k.value for k in ProductSaleKind],
            rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
            fp=fp,
            booking_master_on_ids=master_ids,
            visit_stock_kit_initial=_visit_stock_kit_initial_for_form(db, fp),
            line_service_kits_prefill=_line_service_kits_prefill_for_form(db, fp),
            **_booking_form_consultation_template_ctx(db, fp, b),
        ),
    )
@legacy_bookings_admin_router.post("/{booking_id}/edit")
async def admin_booking_edit_post(
    request: Request,
    booking_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    b = db.scalar(
        select(Booking)
        .where(Booking.id == booking_id)
        .options(
            selectinload(Booking.masters),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.masters),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
        )
    )
    if b is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    form_raw = await request.form()
    fp = {k: form_raw.get(k) for k in form_raw.keys() if not isinstance(form_raw.get(k), UploadFile)}
    visit_order_master_ids: list[int] = []
    for v in form_raw.getlist("visit_order_master_on"):
        try:
            visit_order_master_ids.append(parse_int(v, min=1, field_name="visit_order_master_on"))
        except Exception:
            pass
    visit_order_master_ids = sorted(set([i for i in visit_order_master_ids if i > 0]))
    if visit_order_master_ids:
        fp["visit_order_master_ids"] = ",".join([str(i) for i in visit_order_master_ids])
    else:
        fp.pop("visit_order_master_ids", None)

    client_id_raw = str(fp.get("client_id") or "").strip()
    kind_raw = str(fp.get("kind") or "").strip() or BookingKind.VISIT.value
    quoted_price_text = strip_or_none(str(fp.get("quoted_price_text") or ""), 120)
    deposit_raw = str(fp.get("deposit_amount") or "").strip()
    comment = strip_or_none(str(fp.get("comment") or "")) or None
    photo_1_url = getattr(b, "photo_1", None)
    photo_2_url = getattr(b, "photo_2", None)
    photo_3_url = getattr(b, "photo_3", None)

    err: str | None = None
    client = None
    planned_date = None
    deposit_amount: int | None = None
    planned_service_id: int | None = None
    planned_product_kind: str | None = None
    planned_service_ids: list[int] = []
    planned_service_lines: list[dict[str, Any]] = []
    on_ids: list[int] = []
    tz_name: str | None = None
    new_booking_status: BookingStatus | None = None

    try:
        client_id = parse_int(client_id_raw, min=1, field_name="client_id")
    except ValueError:
        client_id = 0
    if client_id <= 0:
        err = "Выберите клиента."
    else:
        client = db.get(Client, client_id)
        if client is None:
            err = "Клиент не найден."

    if not err and UserRole.ADMIN_SUPER in current_user.roles:
        status_raw = str(fp.get("booking_status") or "").strip() or str(b.status.value)
        try:
            new_booking_status = BookingStatus(status_raw)
        except ValueError:
            err = "Некорректный статус брони."

    if not err:
        try:
            tz_name = get_display_timezone(db)
            planned_date = _parse_planned_booking_datetime(fp, tz_name)
        except Exception:
            err = "Укажите дату и время брони."

    if not err:
        if kind_raw not in _BOOKING_KIND_OPTIONS:
            err = "Некорректный тип брони."

    if not err and deposit_raw:
        try:
            dep = parse_float(deposit_raw, min=0.0, field_name="deposit_amount")
            deposit_amount = int(dep)
        except Exception:
            err = "Предоплата должна быть числом."

    if not err and kind_raw == BookingKind.VISIT.value:
        svc_err, planned_service_ids, planned_service_id, planned_service_lines, on_ids = _parse_booking_visit_lines_and_masters(
            db, form_raw, fp
        )
        if svc_err:
            err = svc_err
        else:
            _apply_parsed_visit_lines_to_fp(fp, planned_service_lines)
            existing_line_count = len(list(b.planned_services or []))
            if existing_line_count > 1 and len(planned_service_lines) < existing_line_count:
                _logger.warning(
                    "booking edit #%s: service line count decreased %s -> %s, service_ids=%s",
                    booking_id,
                    existing_line_count,
                    len(planned_service_lines),
                    [int(x["service_id"]) for x in planned_service_lines],
                )
            _logger.info(
                "booking edit #%s: raw_lines_json_len=%s, parsed %s line(s), service_ids=%s, durations=%s, masters=%s",
                booking_id,
                len(str(fp.get("booking_service_lines_json") or "")),
                len(planned_service_lines),
                [int(x["service_id"]) for x in planned_service_lines],
                [_parse_line_duration_minutes(x.get("duration_minutes")) for x in planned_service_lines],
                on_ids,
            )
        if not err and (fp.get("visit_kit_mode") or "") == "ORDER" and parse_bool(fp.get("visit_order_use_masters")):
            if not visit_order_master_ids:
                err = "Выберите хотя бы одного мастера для заказа комплекта."
        if not err:
            kit_err = _validate_visit_stock_kit_for_reserve(db, fp)
            if kit_err:
                err = kit_err
        if not err:
            line_kit_err = _validate_line_service_kits_for_reserve(db, fp)
            if line_kit_err:
                err = line_kit_err

    if not err and kind_raw == BookingKind.PRODUCT_SALE.value:
        pk = str(fp.get("product_kind") or "").strip()
        if pk not in [k.value for k in ProductSaleKind]:
            err = "Выберите тип продажи."
        else:
            planned_product_kind = pk
            if pk == ProductSaleKind.KIT.value and (fp.get("sale_kit_mode") or "") == "ORDER":
                if len(form_raw.getlist("sale_kit_order_master_on")) == 0:
                    err = "Выберите хотя бы одного мастера для заказа комплекта."
            if pk == ProductSaleKind.RUBBER.value and (fp.get("sale_rubber_mode") or "") == "ORDER":
                raw_mid = str(fp.get("sale_rubber_order_master_id") or "").strip()
                try:
                    _ = parse_int(raw_mid, min=1, field_name="sale_rubber_order_master_id")
                except ValueError:
                    err = "Выберите мастера для заказа хвоста/резинки."

    if not err and kind_raw == BookingKind.CONSULTATION.value:
        cons_err, types_data, planned_service_ids = _parse_booking_consultation_fields(db, form_raw, fp)
        if cons_err:
            err = cons_err
        else:
            fp["consultation_types_json"] = types_json_dumps(types_data)
            planned_service_id = planned_service_ids[0] if planned_service_ids else None

    masters = _masters_for_visit_form(db)
    service_catalog = list_master_visit_services_catalog(db)
    staff_users = list(db.scalars(select_users_with_any_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)).all())
    selected_client = client

    # --- График мастеров (жёсткая server-validation при редактировании) ---
    if (
        not err
        and kind_raw == BookingKind.VISIT.value
        and planned_service_lines
        and planned_date is not None
        and tz_name is not None
        and on_ids
    ):
        local_day = _booking_local_day_from_fp(fp, planned_date_utc=planned_date, tz_name=tz_name)
        dur_override = _booking_custom_duration_override_minutes(fp)
        err = _validate_booking_visit_line_availability(
            db,
            local_day=local_day,
            line_specs=planned_service_lines,
            duration_override_minutes=dur_override,
        )

    if err:
        return templates.TemplateResponse(
            "admin_booking_form.html",
            _ctx(
                request,
                current_user=current_user,
                error=err,
                is_new=False,
                booking=b,
                selected_client=selected_client,
                masters=masters,
                staff_users=staff_users,
                after_reserve=str(request.url),
                service_catalog=service_catalog,
                kind_options=_BOOKING_KIND_OPTIONS,
                product_kind_options=[k.value for k in ProductSaleKind],
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                fp=fp,
                booking_master_on_ids=on_ids,
                visit_stock_kit_initial=_visit_stock_kit_initial_for_form(db, fp),
            line_service_kits_prefill=_line_service_kits_prefill_for_form(db, fp),
            ),
            status_code=400,
        )

    assert client is not None and planned_date is not None
    if UserRole.ADMIN_SUPER in current_user.roles and new_booking_status is not None:
        _apply_super_admin_booking_status_change(db, b, new_booking_status, current_user.id)
    before_visit_master_ids = [int(bm.master_id) for bm in (b.masters or [])]
    before_visit_masters = _audit_user_names(db, before_visit_master_ids)
    before_sale_staff = _audit_sale_order_masters_label(db, b.id)
    before_planned_services = _planned_services_audit_label(db, b.id)
    before_details_json = b.details_json
    before = SimpleNamespace(
        client_id=b.client_id,
        planned_date=b.planned_date.replace(second=0, microsecond=0) if b.planned_date else None,
        kind=b.kind,
        status=b.status,
        quoted_price_text=b.quoted_price_text,
        deposit_amount=b.deposit_amount,
        photo_1=getattr(b, "photo_1", None),
        photo_2=getattr(b, "photo_2", None),
        photo_3=getattr(b, "photo_3", None),
        comment=b.comment,
        planned_service_id=b.planned_service_id,
        planned_product_kind=b.planned_product_kind,
        details_json=_canonical_booking_details_json(b.details_json),
        cancelled_reason=b.cancelled_reason,
    )

    # photos: replace/clear
    try:
        if parse_bool(form_raw.get("clear_photo_1")):
            delete_media_by_url(photo_1_url)
            photo_1_url = None
        if parse_bool(form_raw.get("clear_photo_2")):
            delete_media_by_url(photo_2_url)
            photo_2_url = None
        if parse_bool(form_raw.get("clear_photo_3")):
            delete_media_by_url(photo_3_url)
            photo_3_url = None
        up1 = get_nonempty_upload(form_raw, "photo_1")
        up2 = get_nonempty_upload(form_raw, "photo_2")
        up3 = get_nonempty_upload(form_raw, "photo_3")
        if up1 is not None:
            new_url = await save_upload_image(up1)
            delete_media_by_url(photo_1_url)
            photo_1_url = new_url
        if up2 is not None:
            new_url = await save_upload_image(up2)
            delete_media_by_url(photo_2_url)
            photo_2_url = new_url
        if up3 is not None:
            new_url = await save_upload_image(up3)
            delete_media_by_url(photo_3_url)
            photo_3_url = new_url
    except ValueError as exc:
        err = str(exc)
        masters = _masters_for_visit_form(db)
        service_catalog = list_master_visit_services_catalog(db)
        staff_users = list(db.scalars(select_users_with_any_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)).all())
        selected_client = client
        return templates.TemplateResponse(
            "admin_booking_form.html",
            _ctx(
                request,
                current_user=current_user,
                error=err,
                is_new=False,
                booking=b,
                selected_client=selected_client,
                masters=masters,
                staff_users=staff_users,
                after_reserve=str(request.url),
                service_catalog=service_catalog,
                kind_options=_BOOKING_KIND_OPTIONS,
                product_kind_options=[k.value for k in ProductSaleKind],
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                fp=fp,
                booking_master_on_ids=on_ids,
                visit_stock_kit_initial=_visit_stock_kit_initial_for_form(db, fp),
            line_service_kits_prefill=_line_service_kits_prefill_for_form(db, fp),
            ),
            status_code=400,
        )
    b.client_id = client.id
    b.planned_date = planned_date
    b.kind = BookingKind(kind_raw)
    b.quoted_price_text = quoted_price_text
    b.deposit_amount = deposit_amount
    b.photo_1 = photo_1_url
    b.photo_2 = photo_2_url
    b.photo_3 = photo_3_url
    b.comment = comment
    b.planned_service_id = planned_service_id
    b.planned_product_kind = planned_product_kind
    b.details_json = json.dumps(_booking_details_from_form(db, fp), ensure_ascii=False)
    b.updated_at = utcnow_naive()
    b.updated_by_user_id = current_user.id

    local_day = _booking_local_day_from_fp(fp, planned_date_utc=planned_date, tz_name=tz_name or get_display_timezone(db))
    if kind_raw == BookingKind.VISIT.value and planned_service_lines:
        _sync_booking_planned_services_with_lines(
            db,
            booking_id=b.id,
            local_day=local_day,
            tz_name=tz_name or get_display_timezone(db),
            line_specs=planned_service_lines,
        )
    elif kind_raw == BookingKind.CONSULTATION.value:
        from app.planned_services_db import sync_booking_planned_services

        sync_booking_planned_services(
            db,
            b.id,
            planned_service_ids,
            planned_date=planned_date,
        )
    else:
        db.execute(delete(BookingPlannedService).where(BookingPlannedService.booking_id == b.id))

    db.execute(delete(BookingMaster).where(BookingMaster.booking_id == b.id))
    db.flush()
    db.expire(b, ["masters"])
    if kind_raw == BookingKind.VISIT.value:
        for mid in on_ids:
            if db.get(User, mid) is None:
                continue
            db.add(BookingMaster(booking_id=b.id, master_id=mid))

    _sync_booking_staff_rows_for_sale(db, booking_id=b.id, fp=fp, form_raw=form_raw)
    release_booking_kit_reserves(db, booking_id=b.id, changed_by_user_id=current_user.id)
    if booking_is_open(b.status):
        _apply_booking_auto_reserves(
            db,
            booking_id=b.id,
            booking_client_id=b.client_id,
            fp=fp,
            changed_by_user_id=current_user.id,
        )
    _refresh_sale_order_master_ids_in_fp(db, booking_id=b.id, fp=fp)
    b.details_json = json.dumps(_booking_details_from_form(db, fp), ensure_ascii=False)
    after_details_json = b.details_json
    after_visit_masters = _audit_user_names(db, on_ids if kind_raw == BookingKind.VISIT.value else [])
    after_sale_staff = _audit_sale_order_masters_label(db, b.id)
    after_planned_services = _planned_services_audit_label(db, b.id)
    after_audit = SimpleNamespace(
        client_id=b.client_id,
        planned_date=b.planned_date.replace(second=0, microsecond=0) if b.planned_date else None,
        kind=b.kind,
        status=b.status,
        quoted_price_text=b.quoted_price_text,
        deposit_amount=b.deposit_amount,
        photo_1=getattr(b, "photo_1", None),
        photo_2=getattr(b, "photo_2", None),
        photo_3=getattr(b, "photo_3", None),
        comment=b.comment,
        planned_service_id=b.planned_service_id,
        planned_product_kind=b.planned_product_kind,
        details_json=_canonical_booking_details_json(b.details_json),
        cancelled_reason=b.cancelled_reason,
    )
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            after_audit,
            (
                "client_id",
                "planned_date",
                "kind",
                "status",
                "quoted_price_text",
                "deposit_amount",
                "photo_1",
                "photo_2",
                "photo_3",
                "comment",
                "planned_service_id",
                "planned_product_kind",
                "cancelled_reason",
            ),
        ),
    )
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=_booking_details_audit_changes(db, before_details_json, after_details_json),
    )
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            SimpleNamespace(visit_masters=before_visit_masters, sale_order_masters=before_sale_staff),
            SimpleNamespace(visit_masters=after_visit_masters, sale_order_masters=after_sale_staff),
            ("visit_masters", "sale_order_masters"),
        ),
    )
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            SimpleNamespace(planned_services=before_planned_services),
            SimpleNamespace(planned_services=after_planned_services),
            ("planned_services",),
        ),
    )
    db.commit()
    _logger.info(
        "booking edit #%s saved: planned_services=%s",
        b.id,
        after_planned_services,
    )
    return RedirectResponse(url=f"/bookings/{b.id}", status_code=303)


@router.get("", response_class=HTMLResponse)
def admin_bookings(
    request: Request,
    show: str | None = None,
    mine: str | None = Query(None),
    sort_date: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    show_mode = (show or "").strip().lower() or "active"
    mine_raw = (mine or "").strip().lower()
    sort_date_raw = (sort_date or "").strip().lower()
    sort_date_mode = "desc" if sort_date_raw == "desc" else "asc"
    can_manage = current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER)
    has_admin_roles = UserRole.ADMIN in current_user.roles or UserRole.ADMIN_SUPER in current_user.roles
    if has_admin_roles:
        bookings_mine_only = mine_raw in ("1", "true", "yes", "only")
    else:
        bookings_mine_only = mine_raw in ("1", "true", "yes", "only")
        if mine_raw not in ("0", "false", "no", "all"):
            bookings_mine_only = True

    stmt = (
        select(Booking)
        .options(
            selectinload(Booking.client),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.service)
            .selectinload(Service.subcategory)
            .selectinload(ServiceSubcategory.category),
            selectinload(Booking.planned_service)
            .selectinload(Service.subcategory)
            .selectinload(ServiceSubcategory.category),
        )
        .limit(1000)
    )
    if show_mode != "all":
        stmt = stmt.where(
            Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE))
        )
    if bookings_mine_only:
        stmt = stmt.where(
            or_(
                exists(select(1).where(and_(BookingMaster.booking_id == Booking.id, BookingMaster.master_id == current_user.id))),
                exists(select(1).where(and_(BookingStaff.booking_id == Booking.id, BookingStaff.user_id == current_user.id))),
            )
        )
    if sort_date_mode == "desc":
        stmt = stmt.order_by(Booking.planned_date.desc(), Booking.id.desc())
    else:
        stmt = stmt.order_by(Booking.planned_date.asc(), Booking.id.asc())
    rows = list(db.scalars(stmt).all())
    booking_ids = [int(b.id) for b in rows]
    work_by_booking: dict[int, WorkForInventory] = {}
    visit_by_booking: dict[int, Visit] = {}
    sale_by_booking: dict[int, ProductSale] = {}
    if booking_ids:
        for w in db.scalars(select(WorkForInventory).where(WorkForInventory.booking_id.in_(booking_ids))).all():
            if w.booking_id is not None:
                work_by_booking[int(w.booking_id)] = w
        for v in db.scalars(
            select(Visit)
            .where(Visit.booking_id.in_(booking_ids))
            .options(selectinload(Visit.services))
        ).all():
            if v.booking_id is not None:
                visit_by_booking[int(v.booking_id)] = v
        for s in db.scalars(select(ProductSale).where(ProductSale.booking_id.in_(booking_ids))).all():
            if s.booking_id is not None:
                sale_by_booking[int(s.booking_id)] = s
    row_details = {
        int(b.id): booking_list_detail_parts(
            b,
            linked_work=work_by_booking.get(int(b.id)),
            linked_visit=visit_by_booking.get(int(b.id)),
            linked_sale=sale_by_booking.get(int(b.id)),
            product_kind_label_fn=_product_kind_label,
        )
        for b in rows
    }
    display_tz = get_display_timezone(db)
    return templates.TemplateResponse(
        "admin_bookings.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            row_details=row_details,
            show_mode="all" if show_mode == "all" else "active",
            display_tz=display_tz,
            can_manage=can_manage,
            bookings_mine_only=bookings_mine_only,
            sort_date_mode=sort_date_mode,
        ),
    )


@router.post("/{booking_id}/cancel")
@legacy_bookings_admin_router.post("/{booking_id}/cancel")
def admin_booking_cancel(
    booking_id: int,
    reason: str | None = Form(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    if not booking_is_open(b.status):
        return RedirectResponse(url=f"/bookings/{booking_id}", status_code=303)
    reason_norm = (reason or "").strip()
    if not reason_norm:
        return RedirectResponse(url=f"/bookings/{booking_id}?err=reason", status_code=303)
    before = SimpleNamespace(status=b.status, cancelled_at=b.cancelled_at, cancelled_by_user_id=b.cancelled_by_user_id, cancelled_reason=b.cancelled_reason)
    b.status = BookingStatus.CANCELLED
    b.cancelled_at = utcnow_naive()
    b.cancelled_by_user_id = current_user.id
    b.cancelled_reason = reason_norm[:2000]
    b.updated_at = utcnow_naive()
    b.updated_by_user_id = current_user.id
    if b.consultation_id:
        from app.payroll_fund import storno_source_accruals

        storno_source_accruals(
            db,
            PayrollFundSourceKind.CONSULTATION,
            int(b.consultation_id),
            current_user.id,
        )
        b.consultation_id = None
    release_booking_kit_reserves(db, booking_id=b.id, changed_by_user_id=current_user.id)
    db.commit()
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, b, ("status", "cancelled_at", "cancelled_by_user_id", "cancelled_reason")),
    )
    db.commit()
    return RedirectResponse(url=f"/bookings/{booking_id}", status_code=303)


@router.post("/{booking_id}/mark-done")
@legacy_bookings_admin_router.post("/{booking_id}/mark-done")
def admin_booking_mark_done(
    booking_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    if not booking_is_open(b.status):
        return RedirectResponse(url=f"/bookings/{booking_id}", status_code=303)
    old_status = b.status
    b.status = BookingStatus.DONE
    b.updated_at = utcnow_naive()
    b.updated_by_user_id = current_user.id
    release_booking_kit_reserves(db, booking_id=b.id, changed_by_user_id=current_user.id)
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=[FieldChange("status", _booking_status_label(old_status.value), _booking_status_label(BookingStatus.DONE.value))],
    )
    db.commit()
    return RedirectResponse(url=f"/bookings/{booking_id}", status_code=303)


@router.post("/{booking_id}/confirm")
@legacy_bookings_admin_router.post("/{booking_id}/confirm")
def admin_booking_confirm(
    booking_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    b = db.get(Booking, booking_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Бронь не найдена")
    if b.status != BookingStatus.PENDING_CONFIRMATION:
        return RedirectResponse(url=f"/bookings/{booking_id}", status_code=303)
    old_status = b.status
    b.status = BookingStatus.ACTIVE
    b.updated_at = utcnow_naive()
    b.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=BookingAuditLog,
        entity_field="booking_id",
        entity_id=b.id,
        changed_by_user_id=current_user.id,
        changes=[
            FieldChange(
                "status",
                _booking_status_label(old_status.value),
                _booking_status_label(BookingStatus.ACTIVE.value),
            )
        ],
    )
    db.commit()
    return RedirectResponse(url=f"/bookings/{booking_id}?msg=confirmed", status_code=303)


def _product_sale_activity_label(sale: ProductSale) -> str:
    k = sale.kind
    if k == ProductSaleKind.KIT:
        return "Продажа: комплект"
    if k == ProductSaleKind.RUBBER:
        return "Продажа: хвост/резинка"
    if k == ProductSaleKind.MATERIAL:
        return "Продажа: материал"
    if k == ProductSaleKind.OTHER:
        return "Продажа: другое"
    return "Продажа"


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


def _master_activity_archive_row_id(row: dict[str, Any]) -> int:
    if row["kind"] == "visit":
        return row["visit"].id
    if row["kind"] == "work":
        return row["work"].id
    return row["sale"].id


def master_activity_archive(db: Session, master_id: int, *, days: int = 30, max_rows: int = 50) -> tuple[list[dict[str, Any]], bool]:
    cutoff = utcnow_naive() - timedelta(days=days)
    items: list[dict[str, Any]] = []

    visits = list(
        db.scalars(
            select(Visit)
            .where(
                Visit.performed_date >= cutoff,
                Visit.is_cancelled.is_(False),
                or_(
                    Visit.id.in_(select(VisitMaster.visit_id).where(VisitMaster.master_id == master_id)),
                    Visit.mix_bonus_master_id == master_id,
                ),
            )
            .options(selectinload(Visit.client), selectinload(Visit.services))
        ).all()
    )
    for v in visits:
        svc_l = sorted(v.services, key=lambda s: s.id)[0].service_name if v.services else "Визит"
        items.append({"kind": "visit", "sort_at": v.performed_date, "visit": v, "label": svc_l, "client": v.client})

    works = list(
        db.scalars(
            select(WorkForInventory)
            .where(
                WorkForInventory.created_at >= cutoff,
                WorkForInventory.is_voided.is_(False),
                or_(
                    WorkForInventory.created_by_user_id == master_id,
                    WorkForInventory.id.in_(select(WorkForInventoryStaff.work_id).where(WorkForInventoryStaff.user_id == master_id)),
                ),
            )
            .options(selectinload(WorkForInventory.client), selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user))
        ).all()
    )
    for w in works:
        items.append({"kind": "work", "sort_at": w.created_at, "work": w, "label": _work_activity_label(w), "client": w.client})

    staff_on_booking = exists(select(1).where(BookingStaff.booking_id == ProductSale.booking_id, BookingStaff.user_id == master_id))
    sales = list(
        db.scalars(
            select(ProductSale)
            .where(
                ProductSale.performed_date >= cutoff,
                ProductSale.is_voided.is_(False),
                or_(
                    ProductSale.created_by_user_id == master_id,
                    ProductSale.material_mix_bonus_user_id == master_id,
                    staff_on_booking,
                ),
            )
            .options(selectinload(ProductSale.client))
        ).all()
    )
    for s in sales:
        items.append({"kind": "sale", "sort_at": s.performed_date, "sale": s, "label": _product_sale_activity_label(s), "client": s.client})

    items.sort(key=lambda r: (r["sort_at"], str(r["kind"]), _master_activity_archive_row_id(r)), reverse=True)
    truncated = len(items) > max_rows
    return items[:max_rows], truncated


# --- Старые GET-URL: /admin/bookings/... -> 308 -> /bookings/... (query сохраняется) ---


@legacy_bookings_admin_router.get("/{booking_id}/edit", response_class=HTMLResponse)
def admin_booking_edit_get_legacy_redirect(
    booking_id: int,
    request: Request,
    current_user: AuthUser = _BOOKINGS_ADMINS,
):
    return _redirect_admin_bookings_to_canon(request, suffix=f"/{int(booking_id)}/edit")


@legacy_bookings_admin_router.get("/new", response_class=HTMLResponse)
def admin_booking_new_get_legacy_redirect(
    request: Request,
    current_user: AuthUser = _BOOKINGS_ADMINS,
):
    return _redirect_admin_bookings_to_canon(request, suffix="/new")


@legacy_bookings_admin_router.get("/{booking_id}", response_class=HTMLResponse)
def admin_booking_detail_legacy_redirect(
    booking_id: int,
    request: Request,
    current_user: AuthUser = _BOOKINGS_STAFF,
):
    return _redirect_admin_bookings_to_canon(request, suffix=f"/{int(booking_id)}")


@legacy_bookings_admin_router.get("", response_class=HTMLResponse)
def admin_bookings_list_legacy_redirect(
    request: Request,
    current_user: AuthUser = _BOOKINGS_STAFF,
):
    return _redirect_admin_bookings_to_canon(request)


@master_bookings_page_router.get("", response_class=HTMLResponse)
def master_bookings(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    from app.visit_draft import draft_summary_label, list_open_drafts_for_master, preview_dict_from_json

    draft_rows: list[dict[str, object]] = []
    for d in list_open_drafts_for_master(db, current_user.id):
        preview = preview_dict_from_json(d.preview_json)
        draft_rows.append(
            {
                "draft": d,
                "services_label": draft_summary_label(preview),
                "amount_total": preview.get("amount_from_client_total"),
            }
        )
    visit_ids = list(
        db.scalars(
            select(Booking.id)
            .join(BookingMaster, BookingMaster.booking_id == Booking.id)
            .where(
                Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)),
                BookingMaster.master_id == current_user.id,
            )
        ).all()
    )
    sale_ids = list(
        db.scalars(
            select(Booking.id)
            .join(BookingStaff, BookingStaff.booking_id == Booking.id)
            .where(
                Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)),
                BookingStaff.user_id == current_user.id,
            )
        ).all()
    )
    ids = sorted(set([int(x) for x in (visit_ids + sale_ids) if x is not None]))
    display_tz = get_display_timezone(db)

    def _sale_order_label(b: Booking) -> str:
        try:
            d = json.loads(b.details_json or "{}")
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        pk = (b.planned_product_kind or "").strip()
        if pk == ProductSaleKind.KIT.value:
            if (d.get("sale_kit_mode") or "") == "ORDER":
                return "Заказ: комплект"
            return "Продажа: комплект (из наличия)"
        if pk == ProductSaleKind.RUBBER.value:
            if (d.get("sale_rubber_mode") or "") == "ORDER":
                return "Заказ: хвост/резинка"
            return "Продажа: хвост/резинка (из наличия)"
        if pk == ProductSaleKind.MATERIAL.value:
            return "Продажа: материал"
        if pk == ProductSaleKind.OTHER.value:
            return "Продажа: другое"
        return "Продажа"

    rows: list[dict[str, object]] = []
    if ids:
        stmt = (
            select(Booking)
            .where(Booking.id.in_(ids))
            .options(selectinload(Booking.client), selectinload(Booking.planned_service).selectinload(Service.subcategory))
            .order_by(Booking.planned_date.asc(), Booking.id.asc())
        )
        bookings = list(db.scalars(stmt).all())
        for b in bookings:
            if b.kind == BookingKind.VISIT:
                if b.planned_service:
                    label = f"{b.planned_service.subcategory.name} — {b.planned_service.name}"
                else:
                    label = "Визит"
            else:
                label = _sale_order_label(b)
            rows.append({"booking": b, "label": label})
    archive_days = 30
    archive_cap = 50
    archive_rows, archive_truncated = master_activity_archive(db, current_user.id, days=archive_days, max_rows=archive_cap)
    return templates.TemplateResponse(
        "master_bookings.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            draft_rows=draft_rows,
            display_tz=display_tz,
            archive_rows=archive_rows,
            archive_days=archive_days,
            archive_cap=archive_cap,
            archive_truncated=archive_truncated,
        ),
    )

