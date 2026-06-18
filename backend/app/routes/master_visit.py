from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.client_validation import format_created_by_label
from app.db.models import (
    Booking,
    BookingKind,
    Client,
    Kit,
    KitReserve,
    MaterialPriceCurrent,
    MaterialType,
    Service,
    User,
    UserRole,
    Visit,
    VisitDraft,
    WorkForInventory,
    WorkKind,
)
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.planned_service_time import planned_start_local_datetime
from app.forms_parse import parse_int
from app.media_store import get_nonempty_upload, save_upload_image
from app.kit_inlay_visit import (
    AMORTIZATION_LEVEL_RUBLES,
    collect_questionnaire_prefill_from_form,
    get_salon_cut_pct,
    kit_reserve_hint_by_id,
    kit_suggest_dict_for_kit_id,
    list_master_visit_services_catalog,
    master_visit_step1_prefill_from_form,
    parse_kit_inlay_form,
    save_kit_inlay_visit,
)
from app.visit_multi_service import (
    form_uses_multi_service_lines,
    parse_multi_service_visit_form,
    save_visit_with_services,
)
from app.visit_draft import (
    acquire_draft_lock,
    collect_form_dict,
    finalize_visit_draft,
    form_dict_from_json,
    parse_draft_form,
    release_draft_lock,
    save_visit_draft,
    user_can_edit_draft,
    user_can_view_draft,
)
from app.planned_services_db import booking_planned_service_ids
from app.mix_rates import mix_rates_meta_json_dict
from app.ru_labels import ru_master_level, ru_user_role
from app.routes.bookings import try_auto_complete_booking
from app.thermo_visit import collect_thermo_prefill_from_form
from app.user_roles import select_users_with_role
from app.webui import templates, ctx as _ctx


router = APIRouter()
_logger = logging.getLogger("livingbraiding.app")


def _log_visit_new_validation_error(
    request: Request,
    exc: ValueError,
    *,
    current_user: AuthUser,
) -> None:
    msg = str(exc)
    request.state.validation_error = msg
    _logger.warning(
        'POST /master/visit/new validation failed: %s | user=%s',
        msg,
        current_user.username,
    )


def _utc_naive_to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


def _masters_for_visit_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_role(UserRole.MASTER).order_by(User.display_name.asc(), User.username.asc())
        ).all()
    )


def _kit_stock_label_from_form(db: Session, form_map: dict[str, str], field: str) -> str | None:
    raw = (form_map.get(field) or "").strip()
    try:
        kid = parse_int(raw, min=1, field_name=field)
    except ValueError:
        return None
    k = db.get(Kit, kid)
    if not k:
        return None
    return f"{k.sku} — {k.title} (остаток {k.pieces_available})"


def _kit_reserve_hint_from_form(db: Session, form_map: dict[str, str], field: str) -> str | None:
    raw = (form_map.get(field) or "").strip()
    try:
        kid = parse_int(raw, min=1, field_name=field)
    except ValueError:
        return None
    return kit_reserve_hint_by_id(db, kid)


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


def _visit_kit_preview_client_id(
    form_prefill: dict[str, str], selected_client: Client | None
) -> int | None:
    if selected_client is not None:
        return int(selected_client.id)
    raw = (form_prefill.get("existing_client_id") or "").strip()
    try:
        return parse_int(raw, min=1, field_name="existing_client_id")
    except ValueError:
        return None


def _prefill_visit_stock_kit_from_booking(db: Session, b: Booking, form_prefill: dict[str, str]) -> None:
    vs = (form_prefill.get("visit_stock_kit_id") or "").strip()
    try:
        vs_int = parse_int(vs, min=1, field_name="visit_stock_kit_id")
    except ValueError:
        vs_int = 0
    if vs_int > 0 and not (form_prefill.get("stock_kit_id") or "").strip():
        form_prefill["stock_kit_id"] = str(vs_int)
    vp = (form_prefill.get("visit_stock_kit_pieces") or "").strip()
    try:
        vp_int = parse_int(vp, min=1, field_name="visit_stock_kit_pieces")
    except ValueError:
        vp_int = 0
    if vp_int > 0 and not (form_prefill.get("stock_blanks_used") or "").strip():
        form_prefill["stock_blanks_used"] = str(vp_int)
    try:
        _ = parse_int((form_prefill.get("stock_kit_id") or "").strip(), min=1, field_name="stock_kit_id")
        return
    except ValueError:
        pass
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
    try:
        _ = parse_int((form_prefill.get("stock_blanks_used") or "").strip(), min=1, field_name="stock_blanks_used")
        return
    except ValueError:
        pass
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


def _stock_kit_lines_initial_for_template(fp: dict[str, str]) -> list[dict[str, Any]]:
    raw = (fp.get("stock_kit_lines_json") or "").strip()
    if raw:
        try:
            arr = json.loads(raw)
            if isinstance(arr, list) and arr:
                return [dict(x) for x in arr if isinstance(x, dict)]
        except Exception:
            pass
    sk = (fp.get("stock_kit_id") or "").strip()
    if sk.isdigit() and int(sk) > 0:
        bd_raw = (fp.get("stock_breakdown_json") or "").strip()
        breakdown = None
        if bd_raw:
            try:
                breakdown = json.loads(bd_raw)
                if not isinstance(breakdown, dict):
                    breakdown = None
            except Exception:
                breakdown = None
        bu = 0
        if str(fp.get("stock_blanks_used") or "").strip().isdigit():
            bu = int(fp["stock_blanks_used"])
        return [
            {
                "kit_id": int(sk),
                "use_entire": fp.get("stock_use_entire") == "on",
                "blanks_used": bu,
                "breakdown": breakdown,
            }
        ]
    return [{"kit_id": None, "use_entire": False, "blanks_used": 0, "breakdown": None}]


def _visit_master_state_from_prefill(fp: dict[str, str]) -> tuple[list[int], dict[int, str]]:
    vm_on_ids: list[int] = []
    raw = (fp.get("visit_master_on") or "").strip()
    if raw:
        for part in raw.split(","):
            p = part.strip()
            if p.isdigit():
                vm_on_ids.append(int(p))
    vm_pct_str: dict[int, str] = {}
    for key, val in fp.items():
        if not key.startswith("visit_master_pct_"):
            continue
        try:
            mid = int(key.replace("visit_master_pct_", "", 1))
        except ValueError:
            continue
        vm_pct_str[mid] = val
    return vm_on_ids, vm_pct_str


def _master_visit_step1_template_response(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    form_prefill: dict[str, str],
    visit_master_on_ids: list[int],
    visit_master_pct_str: dict[int, str],
    error: str | None = None,
    saved: str | None = None,
    saved_draft_client: bool = False,
    selected_client: Client | None = None,
    default_date: str | None = None,
    status_code: int = 200,
    is_draft: bool = False,
    draft_id: int | None = None,
    draft_readonly: bool = False,
    lock_banner: dict[str, str] | None = None,
    draft_saved: bool = False,
    is_edit: bool = False,
    edit_visit_id: int | None = None,
    cancel_url: str | None = None,
    visit_photos: list[str] | None = None,
):
    performed = (form_prefill.get("performed_date") or "").strip() or (default_date or date.today().isoformat())
    salon_cut_pct = get_salon_cut_pct(db, current_user.id)
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    material_price_per_gram = {
        "kanekalon": float(pk.price_per_gram) if pk else 0.0,
        "kudri": float(pku.price_per_gram) if pku else 0.0,
    }
    cid_prev = _visit_kit_preview_client_id(form_prefill, selected_client)
    stock_lines_initial = _stock_kit_lines_initial_for_template(form_prefill)
    stock_kit_lines_preview: list[dict[str, Any] | None] = []
    for item in stock_lines_initial:
        kid = item.get("kit_id")
        try:
            ik = int(kid) if kid is not None else 0
        except (TypeError, ValueError):
            ik = 0
        if ik > 0:
            stock_kit_lines_preview.append(
                kit_suggest_dict_for_kit_id(db, ik, for_client_id=cid_prev)
            )
        else:
            stock_kit_lines_preview.append(None)
    stock_kit_preview: dict[str, Any] | None = None
    sk_raw = (form_prefill.get("stock_kit_id") or "").strip()
    if sk_raw.isdigit():
        stock_kit_preview = kit_suggest_dict_for_kit_id(db, int(sk_raw), for_client_id=cid_prev)
    extra_stock_kit_preview: dict[str, Any] | None = None
    ex_raw = (form_prefill.get("own_extra_stock_kit_id") or "").strip()
    if ex_raw.isdigit():
        extra_stock_kit_preview = kit_suggest_dict_for_kit_id(db, int(ex_raw), for_client_id=cid_prev)
    return templates.TemplateResponse(
        "master_visit_step1.html",
        _ctx(
            request,
            current_user=current_user,
            service_catalog=list_master_visit_services_catalog(db),
            masters_for_visit=_masters_for_visit_form(db),
            visit_master_on_ids=visit_master_on_ids,
            visit_master_pct_str=visit_master_pct_str,
            stock_kit_selected_label=_kit_stock_label_from_form(db, form_prefill, "stock_kit_id"),
            stock_kit_reserve_hint=_kit_reserve_hint_from_form(db, form_prefill, "stock_kit_id"),
            extra_stock_kit_selected_label=_kit_stock_label_from_form(db, form_prefill, "own_extra_stock_kit_id"),
            extra_stock_kit_reserve_hint=_kit_reserve_hint_from_form(db, form_prefill, "own_extra_stock_kit_id"),
            stock_kit_preview=stock_kit_preview,
            stock_lines_initial=stock_lines_initial,
            stock_kit_lines_preview=stock_kit_lines_preview,
            extra_stock_kit_preview=extra_stock_kit_preview,
            salon_cut_pct=salon_cut_pct,
            material_price_per_gram_json=json.dumps(material_price_per_gram, ensure_ascii=False),
            mix_complexity_rates_json=json.dumps(mix_rates_meta_json_dict(db), ensure_ascii=False),
            amortization_rubles=AMORTIZATION_LEVEL_RUBLES,
            amortization_rubles_json=json.dumps(AMORTIZATION_LEVEL_RUBLES, ensure_ascii=False),
            visit_master_level_ru=ru_master_level(current_user.master_level),
            default_date=performed,
            form_prefill=form_prefill,
            selected_client=selected_client,
            error=error,
            saved=saved,
            saved_draft_client=saved_draft_client,
            is_draft=is_draft,
            draft_id=draft_id,
            draft_readonly=draft_readonly,
            lock_banner=lock_banner,
            draft_saved=draft_saved,
            is_edit=is_edit,
            edit_visit_id=edit_visit_id,
            cancel_url=cancel_url,
            visit_photos=visit_photos or [],
        ),
        status_code=status_code,
    )


def _draft_form_response_from_error(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    form: Any,
    draft_id: int | None,
    draft_readonly: bool,
    lock_banner: dict[str, str] | None,
    error: str,
):
    fp = collect_form_dict(form)
    fp.update(collect_questionnaire_prefill_from_form(form))
    fp.update(collect_thermo_prefill_from_form(form))
    vm_on_ids, vm_pct_str = _visit_master_state_from_prefill(fp)
    selected_client = None
    eid = (fp.get("existing_client_id") or "").strip()
    try:
        eid_int = parse_int(eid, min=1, field_name="existing_client_id")
    except ValueError:
        eid_int = 0
    if eid_int > 0:
        selected_client = db.get(Client, eid_int)
    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill=fp,
        visit_master_on_ids=vm_on_ids,
        visit_master_pct_str=vm_pct_str,
        selected_client=selected_client,
        error=error,
        status_code=400,
        is_draft=True,
        draft_id=draft_id,
        draft_readonly=draft_readonly,
        lock_banner=lock_banner,
    )


def _load_draft_form_context(
    db: Session,
    draft: VisitDraft,
    *,
    current_user: AuthUser,
    acquire_lock: bool,
) -> tuple[dict[str, str], list[int], dict[int, str], Client | None, bool, dict[str, str] | None]:
    lock_banner: dict[str, str] | None = None
    readonly = current_user.role != UserRole.MASTER
    if acquire_lock and current_user.role == UserRole.MASTER:
        lock = acquire_draft_lock(db, draft, current_user.id)
        readonly = lock.readonly
        if lock.lock_holder:
            lock_banner = {
                "display_name": lock.lock_holder.display_name or lock.lock_holder.username,
                "role": ru_user_role(lock.lock_holder.role),
            }
    elif current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER):
        readonly = True
        if draft.locked_by_user_id:
            holder = db.get(User, int(draft.locked_by_user_id))
            if holder:
                lock_banner = {
                    "display_name": holder.display_name or holder.username,
                    "role": ru_user_role(holder.role),
                }
    fp = form_dict_from_json(draft.form_json)
    if not fp.get("performed_date") and draft.performed_date:
        tz = get_display_timezone(db)
        local_dt = _utc_naive_to_local(draft.performed_date, tz)
        if local_dt:
            fp["performed_date"] = local_dt.date().isoformat()
    fp.setdefault("existing_client_id", str(draft.client_id))
    fp.setdefault("client_mode", "existing")
    if draft.booking_id:
        fp.setdefault("booking_id", str(draft.booking_id))
    vm_on_ids, vm_pct_str = _visit_master_state_from_prefill(fp)
    client = db.get(Client, int(draft.client_id))
    return fp, vm_on_ids, vm_pct_str, client, readonly, lock_banner


@router.get("/master/visit/new", response_class=HTMLResponse)
def master_visit_new_get(
    request: Request,
    saved: str | None = None,
    booking_id: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    saved_draft_client = False
    try:
        vid = parse_int(saved, min=1, field_name="saved") if saved else 0
    except ValueError:
        vid = 0
    if vid > 0:
        v = db.scalar(select(Visit).where(Visit.id == vid).options(selectinload(Visit.client)))
        if v and v.client and not v.client.is_confirmed:
            saved_draft_client = True
    form_prefill: dict[str, str] = {}
    selected_client = None
    default_date = date.today().isoformat()
    if booking_id:
        b = db.scalar(
            select(Booking)
            .where(Booking.id == int(booking_id))
            .options(selectinload(Booking.client), selectinload(Booking.planned_services))
        )
        if b and b.client:
            form_prefill["client_mode"] = "existing"
            form_prefill["existing_client_id"] = str(b.client_id)
            selected_client = b.client
            svc_ids = booking_planned_service_ids(db, b.id)
            if svc_ids:
                form_prefill["service_id"] = str(svc_ids[0])
                import json as _json

                form_prefill["planned_service_ids"] = _json.dumps(svc_ids)
                for i, sid in enumerate(svc_ids):
                    form_prefill[f"line_{i}_service_id"] = str(sid)
            if b.planned_services:
                tz = get_display_timezone(db)
                ps_rows = sorted(
                    list(b.planned_services or []),
                    key=lambda x: (int(x.sort_order or 0), int(x.id or 0)),
                )
                for i, ps in enumerate(ps_rows):
                    if i <= 0:
                        continue
                    if ps.comment:
                        form_prefill[f"line_{i}_comment"] = str(ps.comment)
                    local_start = (
                        planned_start_local_datetime(
                            ps.planned_start_time,
                            booking_planned_date=b.planned_date,
                            tz_name=tz,
                        )
                        if ps.planned_start_time
                        else None
                    )
                    if local_start:
                        form_prefill[f"line_{i}_started_time"] = local_start.strftime("%H:%M")
            elif b.planned_service_id:
                form_prefill["service_id"] = str(b.planned_service_id)
            form_prefill["booking_id"] = str(b.id)
            tz = get_display_timezone(db)
            local_dt = _utc_naive_to_local(b.planned_date, tz) if b.planned_date else None
            if local_dt:
                default_date = local_dt.date().isoformat()
            amt = _amount_hint_from_booking(b)
            if amt:
                form_prefill["amount_from_client"] = amt
            try:
                d = json.loads(b.details_json or "{}")
                if isinstance(d, dict):
                    for k, v2 in d.items():
                        if str(k).startswith("visit_") or str(k).startswith("corr_"):
                            form_prefill[str(k)] = str(v2)
            except Exception:
                pass
            _prefill_visit_stock_kit_from_booking(db, b, form_prefill)

    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill=form_prefill,
        visit_master_on_ids=[current_user.id],
        visit_master_pct_str={},
        selected_client=selected_client,
        saved=saved,
        saved_draft_client=saved_draft_client,
        default_date=default_date,
    )


@router.post("/master/visit/new")
async def master_visit_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        booking_id_raw = (form.get("booking_id") or "").strip() if hasattr(form.get("booking_id"), "strip") else str(form.get("booking_id") or "").strip()
        booking_id_val = int(booking_id_raw) if booking_id_raw.isdigit() else None
        has_multi = form_uses_multi_service_lines(form)
        if has_multi:
            multi = parse_multi_service_visit_form(
                form,
                single_master_default_id=current_user.id,
                booking_id=booking_id_val,
            )
            visit = save_visit_with_services(
                db,
                current_user.id,
                multi,
                created_by_label=format_created_by_label(current_user),
            )
        else:
            inp = parse_kit_inlay_form(form, single_master_default_id=current_user.id)
            visit = save_kit_inlay_visit(
                db,
                current_user.id,
                inp,
                created_by_label=format_created_by_label(current_user),
            )
        # photos (up to 3)
        try:
            p1 = get_nonempty_upload(form, "photo_1")
            p2 = get_nonempty_upload(form, "photo_2")
            p3 = get_nonempty_upload(form, "photo_3")
            if p1 is not None:
                visit.photo_1 = await save_upload_image(p1)
            if p2 is not None:
                visit.photo_2 = await save_upload_image(p2)
            if p3 is not None:
                visit.photo_3 = await save_upload_image(p3)
            db.commit()
        except ValueError as exc:
            db.rollback()
            _log_visit_new_validation_error(request, exc, current_user=current_user)
            fp, vm_on_ids, vm_pct_str = master_visit_step1_prefill_from_form(form)
            fp.update(collect_questionnaire_prefill_from_form(form))
            fp.update(collect_thermo_prefill_from_form(form))
            selected_client = None
            eid = (fp.get("existing_client_id") or "").strip()
            try:
                eid_int = parse_int(eid, min=1, field_name="existing_client_id")
            except ValueError:
                eid_int = 0
            if eid_int > 0:
                selected_client = db.get(Client, eid_int)
            return _master_visit_step1_template_response(
                request,
                current_user=current_user,
                db=db,
                form_prefill=fp,
                visit_master_on_ids=vm_on_ids,
                visit_master_pct_str=vm_pct_str,
                selected_client=selected_client,
                error=str(exc),
                status_code=400,
            )
        bid_raw = str(form.get("booking_id") or "").strip()
        try:
            bid_int = parse_int(bid_raw, min=1, field_name="booking_id")
        except ValueError:
            bid_int = 0
        if bid_int > 0:
            try:
                visit.booking_id = bid_int
                db.commit()
                try_auto_complete_booking(db, bid_int)
                db.commit()
            except Exception:
                db.rollback()
    except ValueError as exc:
        _log_visit_new_validation_error(request, exc, current_user=current_user)
        fp, vm_on_ids, vm_pct_str = master_visit_step1_prefill_from_form(form)
        fp.update(collect_questionnaire_prefill_from_form(form))
        fp.update(collect_thermo_prefill_from_form(form))
        selected_client = None
        eid = (fp.get("existing_client_id") or "").strip()
        try:
            eid_int = parse_int(eid, min=1, field_name="existing_client_id")
        except ValueError:
            eid_int = 0
        if eid_int > 0:
            selected_client = db.get(Client, eid_int)
        return _master_visit_step1_template_response(
            request,
            current_user=current_user,
            db=db,
            form_prefill=fp,
            visit_master_on_ids=vm_on_ids,
            visit_master_pct_str=vm_pct_str,
            selected_client=selected_client,
            error=str(exc),
            status_code=400,
        )
    url = f"/visits/{visit.id}?msg=created"
    client_row = db.get(Client, visit.client_id) if visit.client_id else None
    if client_row and not client_row.is_confirmed:
        url += "&draft_client=1"
    return RedirectResponse(url=url, status_code=303)


@router.get("/master/visit/draft/new", response_class=HTMLResponse)
def master_visit_draft_new_get(
    request: Request,
    booking_id: int | None = None,
    draft_saved: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form_prefill: dict[str, str] = {}
    selected_client = None
    default_date = date.today().isoformat()
    if booking_id:
        b = db.scalar(
            select(Booking)
            .where(Booking.id == int(booking_id))
            .options(selectinload(Booking.client), selectinload(Booking.planned_services))
        )
        if b and b.client:
            form_prefill["client_mode"] = "existing"
            form_prefill["existing_client_id"] = str(b.client_id)
            selected_client = b.client
            svc_ids = booking_planned_service_ids(db, b.id)
            if svc_ids:
                form_prefill["service_id"] = str(svc_ids[0])
                form_prefill["planned_service_ids"] = json.dumps(svc_ids)
                for i, sid in enumerate(svc_ids):
                    form_prefill[f"line_{i}_service_id"] = str(sid)
            if b.planned_services:
                tz = get_display_timezone(db)
                ps_rows = sorted(
                    list(b.planned_services or []),
                    key=lambda x: (int(x.sort_order or 0), int(x.id or 0)),
                )
                for i, ps in enumerate(ps_rows):
                    if i <= 0:
                        continue
                    if ps.comment:
                        form_prefill[f"line_{i}_comment"] = str(ps.comment)
                    local_start = (
                        planned_start_local_datetime(
                            ps.planned_start_time,
                            booking_planned_date=b.planned_date,
                            tz_name=tz,
                        )
                        if ps.planned_start_time
                        else None
                    )
                    if local_start:
                        form_prefill[f"line_{i}_started_time"] = local_start.strftime("%H:%M")
            elif b.planned_service_id:
                form_prefill["service_id"] = str(b.planned_service_id)
            form_prefill["booking_id"] = str(b.id)
            tz = get_display_timezone(db)
            local_dt = _utc_naive_to_local(b.planned_date, tz) if b.planned_date else None
            if local_dt:
                default_date = local_dt.date().isoformat()
            amt = _amount_hint_from_booking(b)
            if amt:
                form_prefill["amount_from_client"] = amt
            try:
                d = json.loads(b.details_json or "{}")
                if isinstance(d, dict):
                    for k, v2 in d.items():
                        if str(k).startswith("visit_") or str(k).startswith("corr_"):
                            form_prefill[str(k)] = str(v2)
            except Exception:
                pass
            _prefill_visit_stock_kit_from_booking(db, b, form_prefill)

    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill=form_prefill,
        visit_master_on_ids=[current_user.id],
        visit_master_pct_str={},
        selected_client=selected_client,
        default_date=default_date,
        is_draft=True,
        draft_id=None,
        draft_readonly=False,
        lock_banner=None,
        draft_saved=draft_saved == "1",
    )


@router.get("/master/visit/draft/{draft_id}", response_class=HTMLResponse)
def master_visit_draft_get(
    request: Request,
    draft_id: int,
    draft_saved: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    draft = db.scalar(
        select(VisitDraft)
        .where(VisitDraft.id == int(draft_id), VisitDraft.finalized_visit_id.is_(None))
        .options(selectinload(VisitDraft.participants))
    )
    if not draft or not user_can_view_draft(current_user, draft, db):
        return RedirectResponse("/master/bookings", status_code=303)
    fp, vm_on_ids, vm_pct_str, client, readonly, lock_banner = _load_draft_form_context(
        db, draft, current_user=current_user, acquire_lock=True
    )
    if current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER):
        readonly = True
    elif not user_can_edit_draft(current_user, draft, db):
        readonly = True
    db.commit()
    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill=fp,
        visit_master_on_ids=vm_on_ids,
        visit_master_pct_str=vm_pct_str,
        selected_client=client,
        is_draft=True,
        draft_id=int(draft.id),
        draft_readonly=readonly,
        lock_banner=lock_banner,
        draft_saved=draft_saved == "1",
    )


@router.post("/master/visit/draft")
async def master_visit_draft_create_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        booking_id_raw = str(form.get("booking_id") or "").strip()
        booking_id_val = int(booking_id_raw) if booking_id_raw.isdigit() else None
        inp = parse_draft_form(form, booking_id=booking_id_val)
        form_dict = collect_form_dict(form)
        draft = save_visit_draft(
            db,
            None,
            inp,
            current_user.id,
            form_dict,
            created_by_label=format_created_by_label(current_user),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _draft_form_response_from_error(
            request,
            current_user=current_user,
            db=db,
            form=form,
            draft_id=None,
            draft_readonly=False,
            lock_banner=None,
            error=str(exc),
        )
    return RedirectResponse(f"/master/visit/draft/{draft.id}?draft_saved=1", status_code=303)


@router.post("/master/visit/draft/{draft_id}")
async def master_visit_draft_update_post(
    request: Request,
    draft_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    draft = db.get(VisitDraft, int(draft_id))
    readonly = False
    lock_banner = None
    if draft and draft.locked_by_user_id:
        holder = db.get(User, int(draft.locked_by_user_id))
        if holder:
            lock_banner = {
                "display_name": holder.display_name or holder.username,
                "role": ru_user_role(holder.role),
            }
    if not draft or not user_can_edit_draft(current_user, draft, db):
        return RedirectResponse("/master/bookings", status_code=303)
    lock = acquire_draft_lock(db, draft, current_user.id)
    readonly = lock.readonly
    if lock.lock_holder:
        lock_banner = {
            "display_name": lock.lock_holder.display_name or lock.lock_holder.username,
            "role": ru_user_role(lock.lock_holder.role),
        }
    if readonly:
        db.rollback()
        return _draft_form_response_from_error(
            request,
            current_user=current_user,
            db=db,
            form=form,
            draft_id=int(draft_id),
            draft_readonly=True,
            lock_banner=lock_banner,
            error="Черновик сейчас редактирует другой пользователь.",
        )
    try:
        booking_id_raw = str(form.get("booking_id") or "").strip()
        booking_id_val = int(booking_id_raw) if booking_id_raw.isdigit() else None
        inp = parse_draft_form(form, booking_id=booking_id_val)
        form_dict = collect_form_dict(form)
        save_visit_draft(
            db,
            int(draft_id),
            inp,
            current_user.id,
            form_dict,
            created_by_label=format_created_by_label(current_user),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _draft_form_response_from_error(
            request,
            current_user=current_user,
            db=db,
            form=form,
            draft_id=int(draft_id),
            draft_readonly=False,
            lock_banner=lock_banner,
            error=str(exc),
        )
    return RedirectResponse(f"/master/visit/draft/{draft_id}?draft_saved=1", status_code=303)


@router.post("/master/visit/draft/{draft_id}/finalize")
async def master_visit_draft_finalize_post(
    request: Request,
    draft_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    draft = db.get(VisitDraft, int(draft_id))
    lock_banner = None
    if not draft or not user_can_edit_draft(current_user, draft, db):
        return RedirectResponse("/master/bookings", status_code=303)
    lock = acquire_draft_lock(db, draft, current_user.id)
    if lock.readonly:
        db.rollback()
        if lock.lock_holder:
            lock_banner = {
                "display_name": lock.lock_holder.display_name or lock.lock_holder.username,
                "role": ru_user_role(lock.lock_holder.role),
            }
        return _draft_form_response_from_error(
            request,
            current_user=current_user,
            db=db,
            form=form,
            draft_id=int(draft_id),
            draft_readonly=True,
            lock_banner=lock_banner,
            error="Черновик сейчас редактирует другой пользователь.",
        )
    try:
        booking_id_raw = str(form.get("booking_id") or "").strip()
        booking_id_val = int(booking_id_raw) if booking_id_raw.isdigit() else None
        inp = parse_draft_form(form, booking_id=booking_id_val)
        visit_id = finalize_visit_draft(
            db,
            int(draft_id),
            inp,
            current_user.id,
            created_by_label=format_created_by_label(current_user),
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _draft_form_response_from_error(
            request,
            current_user=current_user,
            db=db,
            form=form,
            draft_id=int(draft_id),
            draft_readonly=False,
            lock_banner=lock_banner,
            error=str(exc),
        )
    url = f"/visits/{visit_id}?msg=created"
    client_row = db.get(Client, draft.client_id) if draft else None
    if client_row and not client_row.is_confirmed:
        url += "&draft_client=1"
    return RedirectResponse(url=url, status_code=303)


@router.post("/master/visit/draft/{draft_id}/unlock")
def master_visit_draft_unlock_post(
    draft_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    draft = db.get(VisitDraft, int(draft_id))
    if draft:
        release_draft_lock(db, draft, current_user.id)
        db.commit()
    return RedirectResponse(f"/master/visit/draft/{draft_id}", status_code=303)

