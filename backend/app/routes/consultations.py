from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload
from app.audit import diff_fields, write_audit_rows
from app.auth import AuthUser, require_role
from app.client_validation import strip_or_none
from app.consultation_booking import (
    booking_for_consultation,
    booking_status_label,
    can_create_booking_from_consultation,
)
from app.consultation_types import (
    CONSULTATION_TYPE_CHOICES,
    CONSULTATION_TYPE_OTHER,
    format_types_display,
    list_consultation_services_catalog,
    parse_types_from_form,
    types_json_dumps,
    types_json_loads,
    validate_types_selected,
)
from app.db.models import (
    Booking,
    BookingStatus,
    Client,
    Consultation,
    ConsultationAuditLog,
    ConsultationService,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSourceKind,
    Service,
    UserRole,
)
from app.planned_services_db import (
    consultation_service_ids,
    parse_service_ids_from_form,
    sync_consultation_services,
)
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.forms_parse import parse_int
from app.media_store import delete_media_by_url, get_nonempty_upload, save_upload_image
from app.time_utils import utcnow_naive
from app.webui import templates, ctx as _ctx

router = APIRouter(prefix="/consultations", tags=["consultations"])

_BOOKING_STATUS_FILTERS = ("done", "active", "none_or_cancelled")


def _default_mine_only(current_user: AuthUser) -> bool:
    has_admin = UserRole.ADMIN in current_user.roles or UserRole.ADMIN_SUPER in current_user.roles
    return not has_admin


def _consultations_list_url(*, mine_only: bool, booking_status: str | None) -> str:
    q: dict[str, str] = {}
    if mine_only:
        q["mine"] = "1"
    if booking_status and booking_status in _BOOKING_STATUS_FILTERS:
        q["booking_status"] = booking_status
    return "/consultations" + ("?" + urlencode(q) if q else "")


def _parse_consultation_datetime(date_raw: str, time_raw: str, tz: ZoneInfo) -> datetime:
    d = date.fromisoformat(date_raw.strip())
    t = (time_raw or "").strip() or "00:00"
    parts = t.split(":")
    hour = int(parts[0]) if parts else 0
    minute = int(parts[1]) if len(parts) > 1 else 0
    local = datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _master_may_edit_consultation(current_user: AuthUser, c: Consultation) -> bool:
    if UserRole.ADMIN in current_user.roles or UserRole.ADMIN_SUPER in current_user.roles:
        return True
    return int(c.created_by_user_id) == int(current_user.id)


def _form_types_checked(fp: dict[str, str]) -> list[str]:
    raw = fp.get("consultation_types") or ""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return [x.strip() for x in str(raw).split(",") if x.strip()]


@router.get("", response_class=HTMLResponse)
def consultations_list(
    request: Request,
    mine: str | None = Query(None),
    booking_status: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    mine_raw = (mine or "").strip().lower()
    if mine_raw in ("1", "true", "yes", "only"):
        consultations_mine_only = True
    elif mine_raw in ("0", "false", "no", "all"):
        consultations_mine_only = False
    else:
        consultations_mine_only = _default_mine_only(current_user)

    bs = (booking_status or "").strip().lower()
    if bs not in _BOOKING_STATUS_FILTERS:
        bs = ""

    stmt = (
        select(Consultation)
        .options(
            selectinload(Consultation.client),
            selectinload(Consultation.created_by_user),
            selectinload(Consultation.booking),
        )
        .order_by(Consultation.consultation_date.desc(), Consultation.id.desc())
        .limit(200)
    )
    if consultations_mine_only:
        stmt = stmt.where(Consultation.created_by_user_id == current_user.id)

    if bs == "done":
        stmt = stmt.where(
            exists(
                select(1).where(
                    Booking.consultation_id == Consultation.id,
                    Booking.status == BookingStatus.DONE,
                )
            )
        )
    elif bs == "active":
        stmt = stmt.where(
            exists(
                select(1).where(
                    Booking.consultation_id == Consultation.id,
                    Booking.status == BookingStatus.ACTIVE,
                )
            )
        )
    elif bs == "none_or_cancelled":
        stmt = stmt.where(
            or_(
                ~exists(select(1).where(Booking.consultation_id == Consultation.id)),
                exists(
                    select(1).where(
                        Booking.consultation_id == Consultation.id,
                        Booking.status == BookingStatus.CANCELLED,
                    )
                ),
            )
        )

    rows = list(db.scalars(stmt).all())
    display_tz = get_display_timezone(db)
    row_views = []
    for c in rows:
        b = c.booking
        row_views.append(
            {
                "consultation": c,
                "booking_status_label": booking_status_label(b.status if b else None),
            }
        )

    return templates.TemplateResponse(
        "admin_consultations.html",
        _ctx(
            request,
            current_user=current_user,
            row_views=row_views,
            consultations_mine_only=consultations_mine_only,
            booking_status_filter=bs,
            display_tz=display_tz,
            consultations_url_scope_all=_consultations_list_url(mine_only=False, booking_status=bs or None),
            consultations_url_scope_mine=_consultations_list_url(mine_only=True, booking_status=bs or None),
            consultations_url_bs_all=_consultations_list_url(
                mine_only=consultations_mine_only, booking_status=None
            ),
            consultations_url_bs_done=_consultations_list_url(
                mine_only=consultations_mine_only, booking_status="done"
            ),
            consultations_url_bs_active=_consultations_list_url(
                mine_only=consultations_mine_only, booking_status="active"
            ),
            consultations_url_bs_none=_consultations_list_url(
                mine_only=consultations_mine_only, booking_status="none_or_cancelled"
            ),
            can_manage_bookings=current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER),
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def consultation_new_get(
    request: Request,
    client_id: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    selected_client = db.get(Client, client_id) if client_id else None
    fp: dict[str, str] = {"consultation_date": date.today().isoformat(), "consultation_time": ""}
    return templates.TemplateResponse(
        "admin_consultation_form.html",
        _ctx(
            request,
            current_user=current_user,
            is_new=True,
            consultation=None,
            selected_client=selected_client,
            service_catalog=list_consultation_services_catalog(db),
            type_choices=CONSULTATION_TYPE_CHOICES,
            fp=fp,
            types_data={},
            selected_service_ids=[],
            error=None,
        ),
    )


@router.post("/new")
async def consultation_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form_raw = await request.form()
    fp = {k: form_raw.getlist(k) if k == "consultation_types" else form_raw.get(k) for k in form_raw.keys()}
    if "consultation_types" not in fp:
        fp["consultation_types"] = form_raw.getlist("consultation_types")

    err, client, consultation_dt, duration_minutes, service_id, types_data, service_ids = _parse_consultation_form(
        db, fp, form_raw
    )
    if err:
        cid = str(fp.get("client_id") or "").strip()
        selected_client = db.get(Client, int(cid)) if cid.isdigit() else None
        return templates.TemplateResponse(
            "admin_consultation_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=True,
                consultation=None,
                selected_client=selected_client,
                service_catalog=list_consultation_services_catalog(db),
                type_choices=CONSULTATION_TYPE_CHOICES,
                fp=_fp_to_str(fp),
                types_data=types_data,
                error=err,
            ),
            status_code=400,
        )

    photo_1, photo_2 = await _save_consultation_photos(form_raw, None, None)
    c = Consultation(
        created_at=utcnow_naive(),
        created_by_user_id=current_user.id,
        client_id=client.id,
        consultation_date=consultation_dt,
        duration_minutes=duration_minutes,
        types_json=types_json_dumps(types_data),
        service_id=service_id,
        comment=strip_or_none(str(fp.get("comment") or "")) or None,
        preliminary_cost_text=strip_or_none(str(fp.get("preliminary_cost_text") or ""), 120),
        photo_1=photo_1,
        photo_2=photo_2,
    )
    db.add(c)
    db.flush()
    sync_consultation_services(db, c.id, service_ids)
    db.commit()
    db.refresh(c)
    return RedirectResponse(url=f"/consultations/{c.id}?msg=created", status_code=303)


@router.get("/{consultation_id}", response_class=HTMLResponse)
def consultation_detail(
    consultation_id: int,
    request: Request,
    msg: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    c = db.scalar(
        select(Consultation)
        .options(
            selectinload(Consultation.client),
            selectinload(Consultation.created_by_user),
            selectinload(Consultation.service),
            selectinload(Consultation.booking),
        )
        .where(Consultation.id == consultation_id)
    )
    if not c:
        raise HTTPException(status_code=404, detail="Консультация не найдена")

    display_tz = get_display_timezone(db)
    b = booking_for_consultation(db, c.id) or c.booking
    pay_rows = list(
        db.scalars(
            select(PayrollFundLedger)
            .where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.CONSULTATION,
                PayrollFundLedger.source_id == c.id,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
            )
            .order_by(PayrollFundLedger.id.desc())
        ).all()
    )
    can_edit = _master_may_edit_consultation(current_user, c)
    can_create_booking = (
        current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER)
        and can_create_booking_from_consultation(db, c)
    )

    return templates.TemplateResponse(
        "admin_consultation_detail.html",
        _ctx(
            request,
            current_user=current_user,
            consultation=c,
            types_display=format_types_display(c.types_json),
            booking=b,
            booking_status_label=booking_status_label(b.status if b else None),
            display_tz=display_tz,
            pay_rows=pay_rows,
            msg=msg,
            can_edit=can_edit,
            can_create_booking=can_create_booking,
            can_manage_bookings=current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER),
        ),
    )


@router.get("/{consultation_id}/edit", response_class=HTMLResponse)
def consultation_edit_get(
    consultation_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    c = db.get(Consultation, consultation_id)
    if not c:
        raise HTTPException(status_code=404, detail="Консультация не найдена")
    if not _master_may_edit_consultation(current_user, c):
        raise HTTPException(status_code=403, detail="Нет прав на редактирование")

    tz = get_display_timezone(db)
    local = c.consultation_date.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    types_data = types_json_loads(c.types_json)
    svc_ids = consultation_service_ids(db, c.id)
    import json as _json

    fp = {
        "client_id": str(c.client_id),
        "consultation_date": local.date().isoformat(),
        "consultation_time": local.strftime("%H:%M"),
        "duration_minutes": "" if c.duration_minutes is None else str(c.duration_minutes),
        "service_id": "" if not svc_ids else str(svc_ids[0]),
        "planned_service_ids": _json.dumps(svc_ids),
        "comment": c.comment or "",
        "preliminary_cost_text": c.preliminary_cost_text or "",
    }
    selected_client = db.get(Client, c.client_id)
    return templates.TemplateResponse(
        "admin_consultation_form.html",
        _ctx(
            request,
            current_user=current_user,
            is_new=False,
            consultation=c,
            selected_service_ids=svc_ids,
            selected_client=selected_client,
            service_catalog=list_consultation_services_catalog(db),
            type_choices=CONSULTATION_TYPE_CHOICES,
            fp=fp,
            types_data=types_data,
            error=None,
        ),
    )


@router.post("/{consultation_id}/edit")
async def consultation_edit_post(
    consultation_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    c = db.get(Consultation, consultation_id)
    if not c:
        raise HTTPException(status_code=404, detail="Консультация не найдена")
    if not _master_may_edit_consultation(current_user, c):
        raise HTTPException(status_code=403, detail="Нет прав на редактирование")

    form_raw = await request.form()
    fp = {k: form_raw.getlist(k) if k == "consultation_types" else form_raw.get(k) for k in form_raw.keys()}
    if "consultation_types" not in fp:
        fp["consultation_types"] = form_raw.getlist("consultation_types")

    before = SimpleNamespace(
        client_id=c.client_id,
        consultation_date=c.consultation_date,
        duration_minutes=c.duration_minutes,
        types_json=c.types_json,
        service_id=c.service_id,
        comment=c.comment,
        preliminary_cost_text=c.preliminary_cost_text,
        photo_1=c.photo_1,
        photo_2=c.photo_2,
    )

    err, client, consultation_dt, duration_minutes, service_id, types_data, service_ids = _parse_consultation_form(
        db, fp, form_raw
    )
    if err:
        selected_client = db.get(Client, int(fp.get("client_id") or 0)) if str(fp.get("client_id") or "").isdigit() else None
        return templates.TemplateResponse(
            "admin_consultation_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=False,
                consultation=c,
                selected_client=selected_client,
                service_catalog=list_consultation_services_catalog(db),
                type_choices=CONSULTATION_TYPE_CHOICES,
                fp=_fp_to_str(fp),
                types_data=types_data,
                error=err,
            ),
            status_code=400,
        )

    photo_1, photo_2 = await _save_consultation_photos(
        form_raw, c.photo_1, c.photo_2, existing=c
    )
    c.client_id = client.id
    c.consultation_date = consultation_dt
    c.duration_minutes = duration_minutes
    c.types_json = types_json_dumps(types_data)
    c.service_id = service_id
    c.comment = strip_or_none(str(fp.get("comment") or "")) or None
    c.preliminary_cost_text = strip_or_none(str(fp.get("preliminary_cost_text") or ""), 120)
    c.photo_1 = photo_1
    c.photo_2 = photo_2
    c.updated_at = utcnow_naive()
    c.updated_by_user_id = current_user.id

    write_audit_rows(
        db,
        log_model=ConsultationAuditLog,
        entity_field="consultation_id",
        entity_id=c.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            c,
            (
                "client_id",
                "consultation_date",
                "duration_minutes",
                "types_json",
                "service_id",
                "comment",
                "preliminary_cost_text",
                "photo_1",
                "photo_2",
            ),
        ),
    )
    sync_consultation_services(db, c.id, service_ids)
    db.commit()
    return RedirectResponse(url=f"/consultations/{c.id}", status_code=303)


def _fp_to_str(fp: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in fp.items():
        if isinstance(v, list):
            out[k] = ",".join(str(x) for x in v)
        else:
            out[k] = str(v) if v is not None else ""
    return out


def _parse_consultation_form(db, fp, form_raw):
    err: str | None = None
    client: Client | None = None
    consultation_dt: datetime | None = None
    duration_minutes: int | None = None
    service_id: int | None = None

    cid_raw = str(fp.get("client_id") or "").strip()
    if not cid_raw.isdigit():
        err = "Выберите клиента."
    else:
        client = db.get(Client, int(cid_raw))
        if not client:
            err = "Клиент не найден."

    date_raw = str(fp.get("consultation_date") or "").strip()
    time_raw = str(fp.get("consultation_time") or "").strip()
    if not err:
        try:
            tz = get_display_timezone(db)
            consultation_dt = _parse_consultation_datetime(date_raw, time_raw, tz)
        except Exception:
            err = "Укажите корректную дату консультации."

    dur_raw = str(fp.get("duration_minutes") or "").strip()
    if not err and dur_raw:
        try:
            duration_minutes = parse_int(dur_raw, min=1, max=24 * 60, field_name="duration_minutes")
        except ValueError:
            err = "Длительность: целое число минут."

    types_list = fp.get("consultation_types")
    if not isinstance(types_list, list):
        types_list = _form_types_checked(fp)
    types_data = parse_types_from_form(types_list, str(fp.get("other_text") or ""))
    if not err:
        terr = validate_types_selected(types_data)
        if terr:
            err = terr

    service_ids = parse_service_ids_from_form(form_raw, list_field="planned_service_ids")
    sid_raw = str(fp.get("service_id") or "").strip()
    if not service_ids and sid_raw.isdigit():
        service_ids = [int(sid_raw)]
    if not err and service_ids:
        for sid in service_ids:
            if db.get(Service, sid) is None:
                err = "Услуга не найдена."
                break
        service_id = service_ids[0]

    return err, client, consultation_dt, duration_minutes, service_id, types_data, service_ids


async def _save_consultation_photos(form_raw, photo_1: str | None, photo_2: str | None, existing: Consultation | None = None):
    if existing and parse_bool_form(form_raw.get("clear_photo_1")):
        delete_media_by_url(photo_1)
        photo_1 = None
    if existing and parse_bool_form(form_raw.get("clear_photo_2")):
        delete_media_by_url(photo_2)
        photo_2 = None
    up1 = get_nonempty_upload(form_raw, "photo_1")
    up2 = get_nonempty_upload(form_raw, "photo_2")
    if up1:
        if photo_1:
            delete_media_by_url(photo_1)
        photo_1 = await save_upload_image(up1)
    if up2:
        if photo_2:
            delete_media_by_url(photo_2)
        photo_2 = await save_upload_image(up2)
    return photo_1, photo_2


def parse_bool_form(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")
