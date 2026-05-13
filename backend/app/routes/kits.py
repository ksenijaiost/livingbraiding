from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.audit import diff_fields, write_audit_rows
from app.auth import AuthUser, require_role
from app.db.models import (
    Client,
    Kit,
    KitAuditLog,
    KitAuthorStaff,
    KitBlankStock,
    KitReserve,
    User,
    UserRole,
)
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime, get_display_timezone, timezone_label
from app.forms_parse import parse_bool, parse_int
from app.kit_bulk_import import (
    MAX_BULK_JSON_BYTES,
    MAX_BULK_KITS,
    import_single_kit_row,
    parse_bulk_kits_json,
)
from app.kit_crud import (
    apply_kit_admin_form,
    calc_kit_stock_price_total_from_composition,
    kit_edit_error_prefill,
    kit_new_error_prefill,
    kit_to_form_prefill,
    list_masters_for_kit_author_pick,
    max_kit_discount_percent_allowed,
    parse_discount_percent_from_form,
    parse_kit_admin_form,
    sync_kit_authors,
    validate_kit_admin_form,
)
from app.media_store import delete_media_by_url, get_nonempty_upload, save_upload_image
from app.kit_composition import KIT_INVENTORY_PIECE_EXCLUDE_KEYS, composition_json_from_totals
from app.kit_blank_stock_core import (
    blank_stock_edit_rows_for_kit,
    blank_stock_qty_map,
    build_usage_breakdown_keyed,
    composition_keys_intersection_catalog,
    consume_blank_stock_for_reserve,
    kit_inventory_is_keyed,
    load_catalog_kit_maps,
    max_take_by_key_for_client,
    parse_composition_totals,
    read_blank_stock_qty_from_admin_form,
    replace_blank_stock_for_kit,
    return_reserve_row_to_stock,
    sync_kit_pieces_available_from_blank_lines,
)
from app.kit_inlay_visit import (
    get_kit_max_reserves_per_kit,
    kit_reserve_hint_by_id,
    kit_reserve_slots_used,
    suggest_kits_for_stock,
)
from app.user_roles import select_users_with_any_role, user_has_any_role
from app.webui import templates, ctx as _ctx
from app.time_utils import utcnow_naive
from app.work_products import _kit_de_items, _kit_se_items


router = APIRouter(prefix="/kits", tags=["kits"])
# GET-алиас под старые закладки/ссылки: /admin/kits/... -> 308 -> /kits/...
legacy_kits_admin_router = APIRouter(prefix="/admin/kits", tags=["kits-legacy"])
master_kits_router = APIRouter(prefix="/master/kits", tags=["kits-master"])


def _redirect_admin_kits_to_canon(request: Request, *, suffix: str = "") -> RedirectResponse:
    suf = (suffix or "").strip()
    if suf and not suf.startswith("/"):
        suf = f"/{suf}"
    new_path = f"/kits{suf}"
    return RedirectResponse(url=str(request.url.replace(path=new_path)), status_code=308)


_KITS_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))
_KITS_ADMIN = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER))
_KITS_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


def _kit_qty_prefill_from_admin_fp(fp: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in fp.items():
        if isinstance(k, str) and k.startswith("kit_qty_"):
            out[k] = str(v) if v is not None else "0"
    return out


def kit_admin_new_table_state_json(*, kit_qty_prefill: dict[str, str]) -> str:
    return json.dumps(
        {
            "mode": "admin_kit_new",
            "currentUserId": 0,
            "masters": [{"id": 0, "name": "Количество в комплекте"}],
            "seItems": [{"key": k, "label": lbl} for k, lbl in _kit_se_items()],
            "deItems": [{"key": k, "label": lbl} for k, lbl in _kit_de_items()],
            "prefill": kit_qty_prefill,
            "excludeFromInventoryPieceCount": sorted(KIT_INVENTORY_PIECE_EXCLUDE_KEYS),
        },
        ensure_ascii=False,
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
        rid = parse_int(raw, min=1, field_name=field)
    except ValueError:
        return None
    return kit_reserve_hint_by_id(db, rid)


def _kit_reserve_redirect_base(kit_id: int, form: Any) -> str:
    ar = str(form.get("after_reserve") or "list").strip()
    if ar == "detail":
        return f"/kits/{kit_id}"
    return "/kits"


def _staff_users_for_reserve(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_any_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER).order_by(
                User.display_name.asc()
            )
        ).all()
    )


def _kit_reservation_tooltip(kit: Kit, db: Session) -> str:
    rows = list(kit.reserves or [])
    if not rows:
        return ""
    tz = get_display_timezone(db)
    chunks: list[str] = []
    for r in rows:
        who: list[str] = []
        if r.reserved_for_client:
            who.append(f"клиент: {r.reserved_for_client.name}")
        if r.reserved_for_user:
            who.append(f"сотр.: {r.reserved_for_user.display_name}")
        if r.reserved_by_user:
            who.append(f"забронировал: {r.reserved_by_user.display_name}")
        if r.booking_id:
            who.append(f"бронь #{int(r.booking_id)}")
        when = format_naive_utc_datetime(r.reserved_at, tz)
        chunks.append(
            f"{r.pieces_reserved} шт. ({', '.join(who) if who else 'цель не указана'}) · {when} ({timezone_label(tz)})"
        )
    return " | ".join(chunks)


def _kit_clear_modal_items(kit: Kit, display_tz: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in kit.reserves or []:
        parts: list[str] = []
        if r.reserved_for_client:
            parts.append((r.reserved_for_client.name or "").strip() or "—")
        if r.reserved_for_user:
            parts.append((r.reserved_for_user.display_name or "").strip() or "—")
        target = " · ".join(parts) if parts else "—"
        when = format_naive_utc_datetime(r.reserved_at, display_tz)
        if r.reserved_by_user:
            author = (r.reserved_by_user.display_name or r.reserved_by_user.username or "").strip() or "—"
        else:
            author = "—"
        booking_line = f"бронь #{int(r.booking_id)}" if r.booking_id else None
        out.append(
            {
                "id": r.id,
                "pieces": int(r.pieces_reserved or 0),
                "target": target,
                "when": when,
                "author": author,
                "booking_line": booking_line,
            }
        )
    return out


@master_kits_router.get("/suggest")
def master_kits_suggest(
    q: str = "",
    client_id: int | None = Query(default=None, ge=1),
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    return JSONResponse({"kits": suggest_kits_for_stock(db, q, for_client_id=client_id)})


@router.get("", response_class=HTMLResponse)
def admin_kits_list(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kits = list(
        db.scalars(
            select(Kit)
            .options(
                selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_client),
                selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_user),
                selectinload(Kit.reserves).selectinload(KitReserve.reserved_by_user),
            )
            .order_by(Kit.sku.asc())
        ).all()
    )
    staff_users = _staff_users_for_reserve(db)
    display_tz = get_display_timezone(db)
    kit_rows = [
        {
            "kit": k,
            "reserve_tooltip": _kit_reservation_tooltip(k, db),
            "reserve_slots_used": len(k.reserves or []),
            "clear_modal_items_json": json.dumps(_kit_clear_modal_items(k, display_tz), ensure_ascii=False),
        }
        for k in kits
    ]
    return templates.TemplateResponse(
        "admin_kits.html",
        _ctx(
            request,
            current_user=current_user,
            kit_rows=kit_rows,
            staff_users=staff_users,
            kit_max_reserves=get_kit_max_reserves_per_kit(db),
            msg=msg,
            err=err,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def admin_kit_new_get(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "admin_kit_form.html",
        _ctx(
            request,
            current_user=current_user,
            is_new=True,
            kit=None,
            fp={"kit_author_ids": [], "discount_percent": "0", "blanks_condition": "NEW"},
            form_action="/kits/new",
            error=None,
            staff_for_kit_authors=list_masters_for_kit_author_pick(db),
            computed_stock_price_total=None,
            computed_stock_price_missing_keys=[],
            blank_stock_rows=[],
            kit_table_state_json=kit_admin_new_table_state_json(kit_qty_prefill={}),
        ),
    )


@router.post("/new")
@legacy_kits_admin_router.post("/new")
async def admin_kit_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        d = parse_kit_admin_form(form, for_create=True)
        validate_kit_admin_form(d, for_create=True)
        if db.scalar(select(Kit.id).where(Kit.sku == d.sku)):
            raise ValueError("Комплект с таким артикулом уже есть")
        kit = Kit()
        apply_kit_admin_form(kit, d)
        kit.composition_json = composition_json_from_totals(d.composition_totals)
        try:
            p1 = get_nonempty_upload(form, "photo_1")
            if p1 is not None:
                kit.photo_1 = await save_upload_image(p1)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        db.add(kit)
        db.flush()
        sync_kit_authors(db, kit, form)
        db.commit()
        return RedirectResponse(url=f"/kits/{kit.id}?msg=created", status_code=303)
    except ValueError as exc:
        fp_err = kit_new_error_prefill(form)
        return templates.TemplateResponse(
            "admin_kit_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=True,
                kit=None,
                fp=fp_err,
                form_action="/kits/new",
                error=str(exc),
                staff_for_kit_authors=list_masters_for_kit_author_pick(db),
                computed_stock_price_total=None,
                computed_stock_price_missing_keys=[],
                blank_stock_rows=[],
                kit_table_state_json=kit_admin_new_table_state_json(
                    kit_qty_prefill=_kit_qty_prefill_from_admin_fp(fp_err)
                ),
            ),
            status_code=400,
        )


@router.get("/bulk-import", response_class=HTMLResponse)
def admin_kits_bulk_import_get(
    request: Request,
    current_user: AuthUser = _KITS_SUPER,
):
    return templates.TemplateResponse(
        "admin_kits_bulk_import.html",
        _ctx(
            request,
            current_user=current_user,
            results=None,
            top_error=None,
            payload_prefill="",
            max_bytes=MAX_BULK_JSON_BYTES,
            max_kits=MAX_BULK_KITS,
        ),
    )


@router.post("/bulk-import", response_class=HTMLResponse)
async def admin_kits_bulk_import_post(
    request: Request,
    current_user: AuthUser = _KITS_SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    payload = str(form.get("payload") or "")
    try:
        rows = parse_bulk_kits_json(payload)
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin_kits_bulk_import.html",
            _ctx(
                request,
                current_user=current_user,
                results=None,
                top_error=str(exc),
                payload_prefill=payload,
                max_bytes=MAX_BULK_JSON_BYTES,
                max_kits=MAX_BULK_KITS,
            ),
            status_code=400,
        )
    reserved: set[str] = set()
    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(import_single_kit_row(db, row, reserved_skus=reserved, changed_by_user_id=current_user.id))
    return templates.TemplateResponse(
        "admin_kits_bulk_import.html",
        _ctx(
            request,
            current_user=current_user,
            results=results,
            top_error=None,
            payload_prefill=payload,
            max_bytes=MAX_BULK_JSON_BYTES,
            max_kits=MAX_BULK_KITS,
        ),
    )


@router.get("/{kit_id}", response_class=HTMLResponse)
def admin_kit_detail(
    request: Request,
    kit_id: int,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.scalar(
        select(Kit)
        .options(
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_client),
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_for_user),
            selectinload(Kit.reserves).selectinload(KitReserve.reserved_by_user),
            selectinload(Kit.reserves).selectinload(KitReserve.booking),
            selectinload(Kit.author_staff_links).selectinload(KitAuthorStaff.user),
        )
        .where(Kit.id == kit_id)
    )
    if not kit:
        raise HTTPException(status_code=404, detail="Комплект не найден")
    audit_rows = list(
        db.scalars(
            select(KitAuditLog)
            .options(selectinload(KitAuditLog.changed_by_user))
            .where(KitAuditLog.kit_id == kit_id)
            .order_by(KitAuditLog.changed_at.desc(), KitAuditLog.id.desc())
            .limit(200)
        ).all()
    )
    display_tz = get_display_timezone(db)
    computed_price, computed_missing = calc_kit_stock_price_total_from_composition(db, kit)
    comp_keys = parse_composition_totals(kit)
    composition_blank_stock_warning = (
        bool(comp_keys)
        and int(kit.pieces_available or 0) > 0
        and not kit_inventory_is_keyed(db, int(kit_id))
    )
    return templates.TemplateResponse(
        "admin_kit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            kit=kit,
            composition_blank_stock_warning=composition_blank_stock_warning,
            computed_stock_price_total=computed_price,
            computed_stock_price_missing_keys=computed_missing,
            audit_rows=audit_rows,
            reserve_tooltip=_kit_reservation_tooltip(kit, db),
            staff_users=_staff_users_for_reserve(db),
            kit_max_reserves=get_kit_max_reserves_per_kit(db),
            kit_reserve_slots_used=kit_reserve_slots_used(db, kit_id),
            display_tz=display_tz,
            clear_modal_items_json=json.dumps(_kit_clear_modal_items(kit, display_tz), ensure_ascii=False),
            msg=msg,
            err=err,
        ),
    )


@router.get("/{kit_id}/edit", response_class=HTMLResponse)
def admin_kit_edit_get(
    request: Request,
    kit_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.scalar(select(Kit).options(selectinload(Kit.author_staff_links)).where(Kit.id == kit_id))
    if not kit:
        raise HTTPException(status_code=404, detail="Комплект не найден")
    computed_price, computed_missing = calc_kit_stock_price_total_from_composition(db, kit)
    return templates.TemplateResponse(
        "admin_kit_form.html",
        _ctx(
            request,
            current_user=current_user,
            is_new=False,
            kit=kit,
            fp=kit_to_form_prefill(kit),
            form_action=f"/kits/{kit_id}/edit",
            error=None,
            staff_for_kit_authors=list_masters_for_kit_author_pick(db),
            computed_stock_price_total=computed_price,
            computed_stock_price_missing_keys=computed_missing,
            blank_stock_rows=blank_stock_edit_rows_for_kit(db, kit),
        ),
    )


@router.post("/{kit_id}/discount")
@legacy_kits_admin_router.post("/{kit_id}/discount")
async def admin_kit_discount_post(
    kit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.get(Kit, kit_id)
    if not kit:
        return RedirectResponse(url="/kits?err=" + quote("Комплект не найден", safe=""), status_code=303)
    form = await request.form()
    try:
        discount = parse_discount_percent_from_form(form)
    except ValueError as exc:
        err_q = quote(str(exc), safe="")
        red = str(form.get("redirect_to") or "").strip().lower()
        if red == "detail":
            return RedirectResponse(url=f"/kits/{kit_id}?err={err_q}", status_code=303)
        return RedirectResponse(url="/kits?err=" + err_q, status_code=303)
    price = float(kit.stock_price_total or 0.0)
    red = str(form.get("redirect_to") or "").strip().lower()
    err_no_cost = "Чтобы задать скидку, сначала укажите себестоимость комплекта в карточке (редактирование)."
    ct = kit.cost_total
    if ct is None or float(ct) <= 0:
        if discount > 0:
            err_q = quote(err_no_cost, safe="")
            if red == "detail":
                return RedirectResponse(url=f"/kits/{kit_id}?err={err_q}", status_code=303)
            return RedirectResponse(url="/kits?err=" + err_q, status_code=303)
        before = SimpleNamespace(discount_percent=kit.discount_percent)
        kit.discount_percent = 0
        kit.updated_at = utcnow_naive()
        kit.updated_by_user_id = current_user.id
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=current_user.id,
            changes=diff_fields(before, kit, ("discount_percent",)),
        )
        db.commit()
        if red == "detail":
            return RedirectResponse(url=f"/kits/{kit_id}?msg=saved", status_code=303)
        return RedirectResponse(url="/kits?msg=saved", status_code=303)
    cost = float(ct)
    max_pct = max_kit_discount_percent_allowed(price, cost) if price > 0 else 0
    if price > 0 and discount > max_pct:
        err_q = quote(
            f"Скидка в процентах не больше {max_pct}% (по марже «цена − себестоимость» с ЗП мастеров).",
            safe="",
        )
        if red == "detail":
            return RedirectResponse(url=f"/kits/{kit_id}?err={err_q}", status_code=303)
        return RedirectResponse(url="/kits?err=" + err_q, status_code=303)
    before = SimpleNamespace(discount_percent=kit.discount_percent)
    kit.discount_percent = discount
    kit.updated_at = utcnow_naive()
    kit.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=KitAuditLog,
        entity_field="kit_id",
        entity_id=kit.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, kit, ("discount_percent",)),
    )
    db.commit()
    if red == "detail":
        return RedirectResponse(url=f"/kits/{kit_id}?msg=saved", status_code=303)
    return RedirectResponse(url="/kits?msg=saved", status_code=303)


@router.post("/{kit_id}/edit")
@legacy_kits_admin_router.post("/{kit_id}/edit")
async def admin_kit_edit_post(
    kit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.get(Kit, kit_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Комплект не найден")
    form = await request.form()
    try:
        before = SimpleNamespace(
            sku=kit.sku,
            title=kit.title,
            photo_1=getattr(kit, "photo_1", None),
            description=kit.description,
            notes=kit.notes,
            blank_type_de=kit.blank_type_de,
            blank_type_se=kit.blank_type_se,
            blanks_condition=kit.blanks_condition,
            pieces_total=kit.pieces_total,
            pieces_available=kit.pieces_available,
            stock_price_total=kit.stock_price_total,
            cost_total=kit.cost_total,
            discount_percent=kit.discount_percent,
            author_external=kit.author_external,
            author_staff_ids=sorted([l.user_id for l in (kit.author_staff_links or [])]),
        )
        d = parse_kit_admin_form(form, for_create=False)
        validate_kit_admin_form(d, for_create=False)
        if d.sku != kit.sku:
            oid = db.scalar(select(Kit.id).where(Kit.sku == d.sku, Kit.id != kit.id))
            if oid:
                raise ValueError("Комплект с таким артикулом уже есть")
        apply_kit_admin_form(kit, d)
        try:
            if parse_bool(form.get("clear_photo_1")):
                delete_media_by_url(getattr(kit, "photo_1", None))
                kit.photo_1 = None
            p1 = get_nonempty_upload(form, "photo_1")
            if p1 is not None:
                new_url = await save_upload_image(p1)
                delete_media_by_url(getattr(kit, "photo_1", None))
                kit.photo_1 = new_url
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        sync_kit_authors(db, kit, form)
        blank_qty = read_blank_stock_qty_from_admin_form(form)
        if blank_qty:
            comp = parse_composition_totals(kit)
            _, meta, _ = load_catalog_kit_maps(db)
            allowed = set(composition_keys_intersection_catalog(comp, meta)) if comp else set()
            if not allowed and comp:
                allowed = set(comp.keys())
            if not allowed:
                raise ValueError("Нет ключей состава для остатков по видам (заполните composition_json).")
            replace_blank_stock_for_kit(db, kit, quantities=blank_qty, allowed_keys=allowed)
        after_auth_ids = sorted([l.user_id for l in (kit.author_staff_links or [])])
        kit.updated_at = utcnow_naive()
        kit.updated_by_user_id = current_user.id
        after = SimpleNamespace(
            sku=kit.sku,
            title=kit.title,
            photo_1=getattr(kit, "photo_1", None),
            description=kit.description,
            notes=kit.notes,
            blank_type_de=kit.blank_type_de,
            blank_type_se=kit.blank_type_se,
            blanks_condition=kit.blanks_condition,
            pieces_total=kit.pieces_total,
            pieces_available=kit.pieces_available,
            stock_price_total=kit.stock_price_total,
            cost_total=kit.cost_total,
            discount_percent=kit.discount_percent,
            author_external=kit.author_external,
            author_staff_ids=after_auth_ids,
        )
        ch = diff_fields(
            before,
            after,
            (
                "sku",
                "title",
                "photo_1",
                "description",
                "notes",
                "blank_type_de",
                "blank_type_se",
                "blanks_condition",
                "pieces_total",
                "pieces_available",
                "stock_price_total",
                "cost_total",
                "discount_percent",
                "author_external",
                "author_staff_ids",
            ),
        )
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=current_user.id,
            changes=ch,
        )
        db.commit()
        return RedirectResponse(url=f"/kits/{kit_id}?msg=saved", status_code=303)
    except ValueError as exc:
        fp = kit_edit_error_prefill(form)
        computed_price, computed_missing = calc_kit_stock_price_total_from_composition(db, kit)
        return templates.TemplateResponse(
            "admin_kit_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=False,
                kit=kit,
                fp=fp,
                form_action=f"/kits/{kit_id}/edit",
                error=str(exc),
                staff_for_kit_authors=list_masters_for_kit_author_pick(db),
                computed_stock_price_total=computed_price,
                computed_stock_price_missing_keys=computed_missing,
                blank_stock_rows=blank_stock_edit_rows_for_kit(db, kit),
            ),
            status_code=400,
        )


@router.post("/{kit_id}/reserve")
@legacy_kits_admin_router.post("/{kit_id}/reserve")
async def admin_kit_reserve_post(
    kit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.scalar(select(Kit).options(selectinload(Kit.reserves)).where(Kit.id == kit_id))
    if not kit:
        return RedirectResponse(url="/kits?err=" + quote("Комплект не найден", safe=""), status_code=303)

    form = await request.form()
    redirect_base = _kit_reserve_redirect_base(kit_id, form)
    action = str(form.get("action") or "").strip().lower()
    max_slots = get_kit_max_reserves_per_kit(db)

    def _err(msg: str) -> RedirectResponse:
        return RedirectResponse(url=redirect_base + "?err=" + quote(msg, safe=""), status_code=303)

    if action == "clear_selected":
        if current_user.role not in (UserRole.ADMIN, UserRole.ADMIN_SUPER):
            return _err("Снятие нескольких резервов доступно администратору.")
        raw_ids = form.getlist("reserve_id") if hasattr(form, "getlist") else []
        ids: list[int] = []
        for v in raw_ids:
            if isinstance(v, UploadFile):
                continue
            s = str(v).strip()
            try:
                ids.append(parse_int(s, min=1, field_name="reserve_id"))
            except ValueError:
                continue
        ids = list(dict.fromkeys(ids))
        if not ids:
            return _err("Отметьте хотя бы один резерв.")
        rows: list[KitReserve] = []
        for i in ids:
            row = db.get(KitReserve, i)
            if row is None or row.kit_id != kit.id:
                return _err("Некорректный выбор резервов.")
            rows.append(row)
        before = SimpleNamespace(pieces_available=kit.pieces_available)
        for r in rows:
            return_reserve_row_to_stock(db, kit, r)
            db.delete(r)
        kit.updated_at = utcnow_naive()
        kit.updated_by_user_id = current_user.id
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=current_user.id,
            changes=diff_fields(before, kit, ("pieces_available",)),
        )
        db.commit()
        return RedirectResponse(url=redirect_base + "?msg=cleared", status_code=303)

    if action == "clear":
        rid_raw = str(form.get("reserve_id") or "").strip()
        try:
            rid = parse_int(rid_raw, min=1, field_name="reserve_id")
        except ValueError:
            rid = 0
        if rid > 0:
            row = db.get(KitReserve, rid)
            if not row or row.kit_id != kit.id:
                return _err("Строка резерва не найдена.")
            if current_user.role == UserRole.MASTER and row.reserved_by_user_id != current_user.id:
                return _err("Снять резерв может автор резерва или администратор.")
            before = SimpleNamespace(pieces_available=kit.pieces_available)
            return_reserve_row_to_stock(db, kit, row)
            kit.updated_at = utcnow_naive()
            kit.updated_by_user_id = current_user.id
            db.delete(row)
            write_audit_rows(
                db,
                log_model=KitAuditLog,
                entity_field="kit_id",
                entity_id=kit.id,
                changed_by_user_id=current_user.id,
                changes=diff_fields(before, kit, ("pieces_available",)),
            )
            db.commit()
            return RedirectResponse(url=redirect_base + "?msg=cleared", status_code=303)
        return _err("Укажите один резерв или отметьте строки в форме «Снять».")

    reserve_full = parse_bool(form.get("reserve_full"))
    qty_raw = str(form.get("reserve_pieces") or "").strip()
    raw_j = str(form.get("reserve_breakdown_json") or "").strip()

    cid_raw = str(form.get("reserved_for_client_id") or "").strip()
    uid_raw = str(form.get("reserved_for_user_id") or "").strip()
    cid: int | None
    uid: int | None
    try:
        cid = parse_int(cid_raw, min=1, field_name="reserved_for_client_id")
    except ValueError:
        cid = None
    try:
        uid = parse_int(uid_raw, min=1, field_name="reserved_for_user_id")
    except ValueError:
        uid = None
    if cid is None and uid is None:
        return _err("Укажите клиента и/или сотрудника для резерва.")
    if cid is not None:
        if not db.get(Client, cid):
            return _err("Клиент не найден.")
    if uid is not None:
        u = db.get(User, uid)
        if not u or not user_has_any_role(db, uid, UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
            return _err("Сотрудник не найден.")

    if kit_inventory_is_keyed(db, kit.id):
        if reserve_full and qty_raw and not raw_j:
            return _err("Выберите либо «весь остаток», либо количество, либо отправьте разбивку по видам.")
        if not raw_j and not reserve_full and not qty_raw:
            return _err("Укажите «весь остаток», количество или разбивку по видам (JSON).")
        sm = blank_stock_qty_map(db, kit.id)
        max_by = max_take_by_key_for_client(db, kit=kit, client_id=cid, stock_map=sm)
        if sum(max_by.values()) <= 0:
            return _err("Нет свободного остатка для резерва.")
        usage_by_key: dict[str, int] | None = None
        if raw_j:
            try:
                d = json.loads(raw_j)
                if isinstance(d, dict):
                    usage_by_key = {str(k): int(v) for k, v in d.items() if int(v) > 0}
            except Exception:
                usage_by_key = None
        try:
            blanks_used = parse_int(qty_raw, min=0, field_name="reserve_pieces") if qty_raw else 0
        except ValueError:
            blanks_used = 0
        try:
            bd = build_usage_breakdown_keyed(
                use_entire=reserve_full,
                blanks_used=blanks_used,
                usage_by_key=usage_by_key,
                max_by_key=max_by,
            )
        except ValueError as exc:
            return _err(str(exc))
        n_rows = sum(1 for _k, n in bd.items() if int(n) > 0)
        if n_rows <= 0:
            return _err("Укажите ненулевой резерв.")
        if kit_reserve_slots_used(db, kit.id) + n_rows > max_slots:
            return _err(f"Достигнут лимит резервов на комплект ({max_slots}). Увеличьте лимит в настройках.")
        before = SimpleNamespace(pieces_available=kit.pieces_available)
        for kk, n in bd.items():
            qn = int(n)
            if qn <= 0:
                continue
            consume_blank_stock_for_reserve(db, kit, kit_key=str(kk), qty=qn, sync_after=False)
            db.add(
                KitReserve(
                    kit_id=kit.id,
                    kit_key=str(kk)[:80],
                    pieces_reserved=qn,
                    reserved_at=utcnow_naive(),
                    reserved_by_user_id=current_user.id,
                    reserved_for_client_id=cid,
                    reserved_for_user_id=uid,
                )
            )
        sync_kit_pieces_available_from_blank_lines(db, kit)
        kit.updated_at = utcnow_naive()
        kit.updated_by_user_id = current_user.id
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=current_user.id,
            changes=diff_fields(before, kit, ("pieces_available",)),
        )
        db.commit()
        return RedirectResponse(url=redirect_base + "?msg=reserved", status_code=303)

    if reserve_full and qty_raw:
        return _err("Выберите либо «весь остаток», либо укажите количество заготовок.")
    if not reserve_full and not qty_raw:
        return _err("Укажите «весь остаток» или количество заготовок.")
    avail = int(kit.pieces_available or 0)
    if avail <= 0:
        return _err("Нет свободного остатка для резерва.")
    if kit_reserve_slots_used(db, kit.id) >= max_slots:
        return _err(f"Достигнут лимит резервов на комплект ({max_slots}). Увеличьте лимит в настройках.")

    if reserve_full:
        qty = avail
    else:
        try:
            qty = parse_int(qty_raw, min=1, field_name="reserve_pieces")
        except ValueError:
            return _err("Некорректное количество заготовок.")
    if qty > avail:
        return _err(f"Нельзя зарезервировать больше свободного остатка ({avail}).")

    before = SimpleNamespace(pieces_available=kit.pieces_available)
    kit.pieces_available = avail - qty
    kit.updated_at = utcnow_naive()
    kit.updated_by_user_id = current_user.id
    db.add(
        KitReserve(
            kit_id=kit.id,
            pieces_reserved=qty,
            reserved_at=utcnow_naive(),
            reserved_by_user_id=current_user.id,
            reserved_for_client_id=cid,
            reserved_for_user_id=uid,
        )
    )
    write_audit_rows(
        db,
        log_model=KitAuditLog,
        entity_field="kit_id",
        entity_id=kit.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, kit, ("pieces_available",)),
    )
    db.commit()
    return RedirectResponse(url=redirect_base + "?msg=reserved", status_code=303)


# --- Старые GET-URL: /admin/kits/... -> 308 -> /kits/... (query сохраняется) ---


@legacy_kits_admin_router.get("/bulk-import", response_class=HTMLResponse)
def admin_kits_bulk_import_get_legacy_redirect(
    request: Request,
    current_user: AuthUser = _KITS_SUPER,
):
    return _redirect_admin_kits_to_canon(request, suffix="/bulk-import")


@legacy_kits_admin_router.post("/bulk-import", response_class=HTMLResponse)
def admin_kits_bulk_import_post_legacy_redirect(
    request: Request,
    current_user: AuthUser = _KITS_SUPER,
):
    return _redirect_admin_kits_to_canon(request, suffix="/bulk-import")


@legacy_kits_admin_router.get("/{kit_id}/edit", response_class=HTMLResponse)
def admin_kit_edit_get_legacy_redirect(
    kit_id: int,
    request: Request,
    current_user: AuthUser = _KITS_ADMIN,
):
    return _redirect_admin_kits_to_canon(request, suffix=f"/{int(kit_id)}/edit")


@legacy_kits_admin_router.get("/{kit_id}", response_class=HTMLResponse)
def admin_kit_detail_legacy_redirect(
    kit_id: int,
    request: Request,
    current_user: AuthUser = _KITS_STAFF,
):
    return _redirect_admin_kits_to_canon(request, suffix=f"/{int(kit_id)}")


@legacy_kits_admin_router.get("/new", response_class=HTMLResponse)
def admin_kit_new_get_legacy_redirect(
    request: Request,
    current_user: AuthUser = _KITS_ADMIN,
):
    return _redirect_admin_kits_to_canon(request, suffix="/new")


@legacy_kits_admin_router.get("", response_class=HTMLResponse)
def admin_kits_list_legacy_redirect(
    request: Request,
    current_user: AuthUser = _KITS_STAFF,
):
    return _redirect_admin_kits_to_canon(request)

