from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_admin_super_assigned, require_role
from app.client_validation import (
    CLIENT_AGE_GROUP_OPTIONS,
    client_age_group_label,
    client_db_to_form_dict,
    client_has_any_contact,
    format_client_birth_display,
    format_created_by_label,
    load_client_source_options,
    parse_age_group,
    parse_birth_fields,
    parse_client_source,
    source_extra_option_for_form,
    strip_or_none,
)
from app.forms_parse import parse_bool
from app.db.models import (
    Booking,
    BookingMaster,
    BookingStatus,
    Client,
    ClientAuditLog,
    ClientThermoTemplate,
    UserRole,
    Visit,
    VisitKitUsage,
)
from app.client_export import build_all_clients_csv_bytes
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.audit import diff_fields, write_audit_rows
from app.media_store import delete_media_by_url, get_nonempty_upload, save_upload_image
from app.time_utils import utcnow_naive
from app.ui_visit_display import visit_services_catalog_line
from app.webui import templates, ctx as _ctx


router = APIRouter(prefix="/clients", tags=["clients"])
# GET-алиас: /admin/clients/... -> 308 -> /clients/...
legacy_clients_admin_router = APIRouter(prefix="/admin/clients", tags=["clients-legacy"])


def _redirect_admin_clients_to_canon(request: Request, *, suffix: str = "") -> RedirectResponse:
    suf = (suffix or "").strip()
    if suf and not suf.startswith("/"):
        suf = f"/{suf}"
    new_path = f"/clients{suf}"
    return RedirectResponse(url=str(request.url.replace(path=new_path)), status_code=308)


_CLIENTS_STAFF = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER))
_CLIENTS_ADMINS = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER))
_SUPER_EXPORT = Depends(require_admin_super_assigned())


def _admin_client_form_page(
    request: Request,
    current_user: AuthUser,
    *,
    form: dict,
    error: str | None,
    is_new: bool,
    client_id: int | None = None,
    created_by_display: str | None = None,
    status_code: int = 200,
):
    so = load_client_source_options()
    seo = source_extra_option_for_form(form, so)
    return templates.TemplateResponse(
        "admin_client_form.html",
        _ctx(
            request,
            current_user=current_user,
            is_new=is_new,
            form_action="/clients/new" if is_new else f"/clients/{client_id}/edit",
            page_heading="Новый клиент" if is_new else "Редактирование клиента",
            submit_label="Создать" if is_new else "Сохранить",
            age_options=CLIENT_AGE_GROUP_OPTIONS,
            source_options=so,
            source_extra_option=seo,
            created_by_label=created_by_display,
            form=form,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/export")
def admin_clients_export_csv(
    _current_user: AuthUser = _SUPER_EXPORT,
    db: Session = Depends(get_db),
):
    """Полный список клиентов в CSV (UTF-8 BOM, `;`) — только суперадмин."""
    body = build_all_clients_csv_bytes(db)
    fn = f"clients_{date.today().isoformat()}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fn}"'},
    )


@legacy_clients_admin_router.get("/export")
def admin_clients_export_csv_legacy_redirect(request: Request):
    return _redirect_admin_clients_to_canon(request, suffix="/export")


@router.get("", response_class=HTMLResponse)
def admin_clients(
    request: Request,
    q: str | None = None,
    created: int | None = None,
    updated: int | None = None,
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    """Список клиентов (админ + мастер; создание/редактирование — только админ)."""
    q_norm = (q or "").strip()
    where = []
    if q_norm:
        like = f"%{q_norm}%"
        where.append(
            or_(
                Client.name.ilike(like),
                Client.phone.ilike(like),
                Client.telegram.ilike(like),
                Client.vk.ilike(like),
                Client.instagram.ilike(like),
                Client.other_contact.ilike(like),
            )
        )

    # One row per visit in the join; sum 1 only for real, non-cancelled visits
    visits_count = func.coalesce(func.sum(case((Visit.is_cancelled.is_(False), 1), else_=0)), 0)

    def _nz(col):
        return func.nullif(func.trim(col), "")

    # Phone first, then first non-empty social (same order as coalesce)
    contact_preview = func.coalesce(
        _nz(Client.phone),
        _nz(Client.telegram),
        _nz(Client.vk),
        _nz(Client.instagram),
        _nz(Client.other_contact),
    ).label("contact_preview")

    has_active_booking = func.coalesce(
        func.max(case(((Booking.status == BookingStatus.ACTIVE), 1), else_=0)),
        0,
    ).label("has_active_booking")

    stmt = (
        select(
            Client.id.label("id"),
            Client.name.label("name"),
            Client.is_confirmed.label("is_confirmed"),
            contact_preview,
            visits_count.label("visits_count"),
            has_active_booking,
        )
        .select_from(Client)
        .join(Visit, Visit.client_id == Client.id, isouter=True)
        .join(Booking, Booking.client_id == Client.id, isouter=True)
        .where(*where)
        .group_by(Client.id, Client.name, Client.is_confirmed)
        .order_by(Client.name.asc())
        .limit(500)
    )
    rows = list(db.execute(stmt).mappings().all())
    created_ok = db.get(Client, created) if created is not None else None
    updated_ok = db.get(Client, updated) if updated is not None else None
    return templates.TemplateResponse(
        "admin_clients.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            q=q_norm,
            created_ok=created_ok,
            updated_ok=updated_ok,
        ),
    )


@router.get("/new", response_class=HTMLResponse)
def admin_client_new_get(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
):
    return _admin_client_form_page(
        request,
        current_user,
        form={},
        error=None,
        is_new=True,
    )


@router.post("/new")
@legacy_clients_admin_router.post("/new")
async def admin_client_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form_raw = await request.form()
    form = {k: form_raw.get(k) for k in form_raw.keys() if not isinstance(form_raw.get(k), UploadFile)}
    err: str | None = None

    name = (str(form.get("name") or "")).strip()
    phone = str(form.get("phone") or "")
    telegram = str(form.get("telegram") or "")
    vk = str(form.get("vk") or "")
    instagram = str(form.get("instagram") or "")
    other_contact = str(form.get("other_contact") or "")
    source = str(form.get("source") or "")
    source_other = str(form.get("source_other") or "")
    comment = str(form.get("comment") or "")
    mark_draft = str(form.get("mark_draft") or "")

    if not name:
        err = "Укажите имя клиента."
    elif not client_has_any_contact(phone, telegram, vk, instagram, other_contact):
        err = "Нужен хотя бы один контакт: телефон или любая из соцсетей."

    bd_raw = str(form.get("birth_day") or "")
    bm_raw = str(form.get("birth_month") or "")
    by_raw = str(form.get("birth_year") or "")
    age_raw = str(form.get("age_group") or "")

    birth_day = birth_month = birth_year = None
    age_group = None

    source_parsed: str | None = None
    if not err:
        try:
            birth_day, birth_month, birth_year = parse_birth_fields(bd_raw, bm_raw, by_raw)
            age_group = parse_age_group(age_raw)
            source_parsed = parse_client_source(source)
        except ValueError as exc:
            err = str(exc)

    if err:
        return _admin_client_form_page(
            request,
            current_user,
            form=form,
            error=err,
            is_new=True,
            status_code=400,
        )

    photo_1_url: str | None = None
    photo_2_url: str | None = None
    try:
        p1 = get_nonempty_upload(form_raw, "photo_1")
        p2 = get_nonempty_upload(form_raw, "photo_2")
        if p1 is not None:
            photo_1_url = await save_upload_image(p1)
        if p2 is not None:
            photo_2_url = await save_upload_image(p2)
    except ValueError as exc:
        return _admin_client_form_page(
            request,
            current_user,
            form=form,
            error=str(exc),
            is_new=True,
            status_code=400,
        )

    client = Client(
        name=name[:200],
        phone=strip_or_none(phone, 30),
        photo_1=photo_1_url,
        photo_2=photo_2_url,
        telegram=strip_or_none(telegram, 100),
        vk=strip_or_none(vk, 120),
        instagram=strip_or_none(instagram, 120),
        other_contact=strip_or_none(other_contact, 200),
        age_group=age_group,
        source=source_parsed,
        source_other=strip_or_none(source_other, 200),
        comment=strip_or_none(comment) or None,
        is_confirmed=(not parse_bool(mark_draft)),
        birth_day=birth_day,
        birth_month=birth_month,
        birth_year=birth_year,
        created_by_label=format_created_by_label(current_user),
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return RedirectResponse(url=f"/clients/{client.id}?msg=created", status_code=303)


@router.get("/{client_id}/edit", response_class=HTMLResponse)
def admin_client_edit_get(
    request: Request,
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    form = client_db_to_form_dict(client)
    return _admin_client_form_page(
        request,
        current_user,
        form=form,
        error=None,
        is_new=False,
        client_id=client.id,
        created_by_display=client.created_by_label,
    )


@router.post("/{client_id}/edit")
@legacy_clients_admin_router.post("/{client_id}/edit")
async def admin_client_edit_post(
    request: Request,
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    before = SimpleNamespace(
        **{
            k: getattr(client, k)
            for k in (
                "name",
                "phone",
                "photo_1",
                "photo_2",
                "telegram",
                "vk",
                "instagram",
                "other_contact",
                "age_group",
                "source",
                "source_other",
                "comment",
                "is_confirmed",
                "birth_day",
                "birth_month",
                "birth_year",
            )
        }
    )
    form_raw = await request.form()
    form: dict[str, str] = {}
    for k in form_raw.keys():
        if k == "is_confirmed":
            continue
        v = form_raw.get(k)
        if isinstance(v, UploadFile):
            continue
        form[k] = str(v or "")
    form["is_confirmed"] = "1" if any(parse_bool(v) for v in form_raw.getlist("is_confirmed")) else "0"

    name = (str(form.get("name") or "")).strip()
    phone = str(form.get("phone") or "")
    telegram = str(form.get("telegram") or "")
    vk = str(form.get("vk") or "")
    instagram = str(form.get("instagram") or "")
    other_contact = str(form.get("other_contact") or "")
    source = str(form.get("source") or "")
    source_other = str(form.get("source_other") or "")
    comment = str(form.get("comment") or "")

    err: str | None = None
    if not name:
        err = "Укажите имя клиента."
    elif not client_has_any_contact(phone, telegram, vk, instagram, other_contact):
        err = "Нужен хотя бы один контакт: телефон или любая из соцсетей."

    bd_raw = str(form.get("birth_day") or "")
    bm_raw = str(form.get("birth_month") or "")
    by_raw = str(form.get("birth_year") or "")
    age_raw = str(form.get("age_group") or "")

    birth_day = birth_month = birth_year = None
    age_group = None
    source_parsed: str | None = None
    is_confirmed = form["is_confirmed"] == "1"

    if not err:
        try:
            birth_day, birth_month, birth_year = parse_birth_fields(bd_raw, bm_raw, by_raw)
            age_group = parse_age_group(age_raw)
            source_parsed = parse_client_source(source, legacy_label=client.source)
        except ValueError as exc:
            err = str(exc)

    if err:
        return _admin_client_form_page(
            request,
            current_user,
            form=form,
            error=err,
            is_new=False,
            client_id=client.id,
            created_by_display=client.created_by_label,
            status_code=400,
        )

    client.name = name[:200]
    client.phone = strip_or_none(phone, 30)
    client.telegram = strip_or_none(telegram, 100)
    client.vk = strip_or_none(vk, 120)
    client.instagram = strip_or_none(instagram, 120)
    client.other_contact = strip_or_none(other_contact, 200)
    client.age_group = age_group
    client.source = source_parsed
    client.source_other = strip_or_none(source_other, 200)
    client.comment = strip_or_none(comment) or None
    client.is_confirmed = is_confirmed
    client.birth_day = birth_day
    client.birth_month = birth_month
    client.birth_year = birth_year

    # photos: replace/clear
    clear_p1 = parse_bool(form_raw.get("clear_photo_1"))
    clear_p2 = parse_bool(form_raw.get("clear_photo_2"))
    try:
        if clear_p1:
            delete_media_by_url(getattr(client, "photo_1", None))
            client.photo_1 = None
        if clear_p2:
            delete_media_by_url(getattr(client, "photo_2", None))
            client.photo_2 = None
        up1 = get_nonempty_upload(form_raw, "photo_1")
        up2 = get_nonempty_upload(form_raw, "photo_2")
        if up1 is not None:
            new_url = await save_upload_image(up1)
            delete_media_by_url(getattr(client, "photo_1", None))
            client.photo_1 = new_url
        if up2 is not None:
            new_url = await save_upload_image(up2)
            delete_media_by_url(getattr(client, "photo_2", None))
            client.photo_2 = new_url
    except ValueError as exc:
        return _admin_client_form_page(
            request,
            current_user,
            form=form,
            error=str(exc),
            is_new=False,
            client_id=client.id,
            created_by_display=client.created_by_label,
            status_code=400,
        )

    client.updated_at = utcnow_naive()
    client.updated_by_user_id = current_user.id

    changes = diff_fields(
        before,
        client,
        (
            "name",
            "phone",
            "photo_1",
            "photo_2",
            "telegram",
            "vk",
            "instagram",
            "other_contact",
            "age_group",
            "source",
            "source_other",
            "comment",
            "is_confirmed",
            "birth_day",
            "birth_month",
            "birth_year",
        ),
    )
    write_audit_rows(
        db,
        log_model=ClientAuditLog,
        entity_field="client_id",
        entity_id=client.id,
        changed_by_user_id=current_user.id,
        changes=changes,
    )

    db.commit()
    return RedirectResponse(url=f"/clients?updated={client.id}", status_code=303)


def _form_to_str_map(form) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in form.keys():
        v = form.get(k)
        if isinstance(v, UploadFile):
            continue
        if isinstance(v, (bytes, bytearray)):
            out[k] = v.decode()
        else:
            out[k] = str(v)
    return out


def _client_suggest_items(db: Session, q: str) -> list[dict[str, str | int | bool]]:
    needle = (q or "").strip()
    stmt = select(Client).order_by(Client.name.asc()).limit(30)
    if needle:
        digits = "".join(ch for ch in needle if ch.isdigit())
        # Phone search: ignore common formatting symbols, allow searching by last digits.
        phone_norm = func.replace(
            func.replace(
                func.replace(
                    func.replace(
                        func.replace(func.replace(func.coalesce(Client.phone, ""), "+", ""), " ", ""),
                        "-",
                        "",
                    ),
                    "(",
                    "",
                ),
                ")",
                "",
            ),
            "\t",
            "",
        )
        if digits:
            stmt = stmt.where(or_(Client.name.ilike(f"%{needle}%"), phone_norm.ilike(f"%{digits}%")))
        else:
            stmt = stmt.where(Client.name.ilike(f"%{needle}%"))
    rows = list(db.scalars(stmt).all())
    items: list[dict[str, str | int | bool]] = []
    for c in rows:
        items.append(
            {
                "id": c.id,
                "name": c.name,
                "is_draft": (not c.is_confirmed),
            }
        )
    return items


@router.get("/suggest")
def admin_clients_suggest(
    q: str = "",
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    # Must be defined BEFORE /clients/{client_id}, otherwise "suggest" is parsed as client_id -> 422.
    return JSONResponse({"clients": _client_suggest_items(db, q)})


@router.get("/{client_id}", response_class=HTMLResponse)
def admin_client_detail(
    request: Request,
    client_id: int,
    confirmed: str | None = None,
    msg: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    audit_rows = list(
        db.scalars(
            select(ClientAuditLog)
            .options(selectinload(ClientAuditLog.changed_by_user))
            .where(ClientAuditLog.client_id == client_id)
            .order_by(ClientAuditLog.changed_at.desc(), ClientAuditLog.id.desc())
            .limit(200)
        ).all()
    )

    visits_stmt = (
        select(Visit)
        .where(Visit.client_id == client_id)
        .options(selectinload(Visit.services))
        .order_by(Visit.performed_date.desc())
    )
    visits = list(db.scalars(visits_stmt).all())
    visit_rows = [{"visit": v, "services_line": visit_services_catalog_line(v)} for v in visits]

    kit_stmt = (
        select(VisitKitUsage)
        .join(Visit, VisitKitUsage.visit_id == Visit.id)
        .where(Visit.client_id == client_id)
        .options(selectinload(VisitKitUsage.kit), selectinload(VisitKitUsage.visit))
        .order_by(Visit.performed_date.desc(), VisitKitUsage.id.asc())
    )
    kit_rows = list(db.scalars(kit_stmt).all())

    thermo_tpls = list(
        db.scalars(
            select(ClientThermoTemplate)
            .where(ClientThermoTemplate.client_id == client_id)
            .order_by(ClientThermoTemplate.created_at.desc(), ClientThermoTemplate.id.desc())
        ).all()
    )

    active_bookings = list(
        db.scalars(
            select(Booking)
            .where(Booking.client_id == client_id, Booking.status == BookingStatus.ACTIVE)
            .order_by(Booking.planned_date.asc(), Booking.id.asc())
            .options(selectinload(Booking.masters).selectinload(BookingMaster.master))
            .limit(10)
        ).all()
    )
    inactive_bookings = list(
        db.scalars(
            select(Booking)
            .where(Booking.client_id == client_id, Booking.status.in_([BookingStatus.DONE, BookingStatus.CANCELLED]))
            .order_by(Booking.planned_date.desc(), Booking.id.desc())
            .limit(2)
        ).all()
    )

    show_admin_actions = current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER)
    display_tz = get_display_timezone(db)
    return templates.TemplateResponse(
        "admin_client_detail.html",
        _ctx(
            request,
            current_user=current_user,
            client=client,
            audit_rows=audit_rows,
            visit_rows=visit_rows,
            kit_rows=kit_rows,
            thermo_templates=thermo_tpls,
            active_bookings=active_bookings,
            inactive_bookings=inactive_bookings,
            birth_display=format_client_birth_display(client.birth_day, client.birth_month, client.birth_year),
            age_group_label=client_age_group_label(client.age_group),
            show_admin_actions=show_admin_actions,
            confirmed_banner=confirmed == "1",
            created_banner=msg == "created",
            display_tz=display_tz,
        ),
    )


@router.post("/{client_id}/confirm")
@legacy_clients_admin_router.post("/{client_id}/confirm")
def admin_client_confirm(
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    before = SimpleNamespace(is_confirmed=client.is_confirmed)
    client.is_confirmed = True
    client.updated_at = utcnow_naive()
    client.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ClientAuditLog,
        entity_field="client_id",
        entity_id=client.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, client, ("is_confirmed",)),
    )
    db.commit()
    return RedirectResponse(url=f"/clients/{client_id}?confirmed=1", status_code=303)


# --- Старые GET-URL: /admin/clients/... -> 308 -> /clients/... (query сохраняется) ---


@legacy_clients_admin_router.get("/{client_id}/edit", response_class=HTMLResponse)
def admin_client_edit_get_legacy_redirect(
    client_id: int,
    request: Request,
    current_user: AuthUser = _CLIENTS_ADMINS,
):
    return _redirect_admin_clients_to_canon(request, suffix=f"/{int(client_id)}/edit")


@legacy_clients_admin_router.get("/suggest")
def admin_clients_suggest_legacy_redirect(
    request: Request,
    current_user: AuthUser = _CLIENTS_STAFF,
):
    return _redirect_admin_clients_to_canon(request, suffix="/suggest")


@legacy_clients_admin_router.get("/new", response_class=HTMLResponse)
def admin_client_new_get_legacy_redirect(
    request: Request,
    current_user: AuthUser = _CLIENTS_ADMINS,
):
    return _redirect_admin_clients_to_canon(request, suffix="/new")


@legacy_clients_admin_router.get("/{client_id}", response_class=HTMLResponse)
def admin_client_detail_legacy_redirect(
    client_id: int,
    request: Request,
    current_user: AuthUser = _CLIENTS_STAFF,
):
    return _redirect_admin_clients_to_canon(request, suffix=f"/{int(client_id)}")


@legacy_clients_admin_router.get("", response_class=HTMLResponse)
def admin_clients_list_legacy_redirect(
    request: Request,
    current_user: AuthUser = _CLIENTS_STAFF,
):
    return _redirect_admin_clients_to_canon(request)

