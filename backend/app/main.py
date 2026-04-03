from __future__ import annotations

"""
FastAPI entrypoint (server-side rendered HTML).

This module is intentionally small and “vertical”:
- auth endpoints (login/logout)
- home page for master/admin
- a few admin pages (clients/visits/settings) used to validate the data model

As the project grows, we can split routes into modules (e.g. `routes/admin.py`,
`routes/master.py`) while keeping templates in `app/templates/`.
"""

from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.admin_questionnaire_fields import router as admin_questionnaire_fields_router
from app.admin_service_catalog import router as admin_service_catalog_router
from app.auth import AuthUser, authenticate, get_current_user, login_response, logout_response, require_role
from app.ru_labels import ru_master_level, ru_questionnaire_field_type
from app.display_time import (
    ALLOWED_TIMEZONES,
    ALLOWED_TIMEZONE_IDS,
    format_naive_utc_datetime,
    get_display_timezone,
    timezone_label,
)
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
from app.db.models import (
    Client,
    Kit,
    MaterialPriceCurrent,
    MaterialType,
    Setting,
    User,
    UserRole,
    Visit,
    VisitAuditLog,
    VisitKitUsage,
    VisitMaster,
)
from app.db.session import get_db
from app.kit_crud import (
    apply_kit_admin_form,
    kit_edit_error_prefill,
    kit_new_error_prefill,
    kit_to_form_prefill,
    parse_kit_admin_form,
    validate_kit_admin_form,
)
from app.kit_inlay_visit import (
    kit_reserve_hint_by_id,
    list_kit_inlay_services_catalog,
    parse_kit_inlay_form,
    save_kit_inlay_visit,
    suggest_kits_for_stock,
)
from app.seed import ensure_seed_data
from app.visit_edit_policy import visit_client_change_policy
from app.ui_visit_display import (
    build_service_human_display,
    kit_usages_empty_explanation,
    ru_mix_complexity,
    ru_mix_source,
    ru_price_type,
    visit_services_catalog_line,
)

app = FastAPI(title="livingbraiding")
app.include_router(admin_service_catalog_router)
app.include_router(admin_questionnaire_fields_router)
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_master_level"] = ru_master_level
templates.env.globals["ru_questionnaire_field_type"] = ru_questionnaire_field_type


@app.on_event("startup")
def _startup():
    """Create dev defaults (users/settings) if DB is empty."""
    db = next(get_db())
    try:
        ensure_seed_data(db)
    finally:
        db.close()


def _ctx(request: Request, current_user=None, **kwargs):
    """Common Jinja context: always pass request + current_user."""
    return {"request": request, "current_user": current_user, **kwargs}


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
            form_action="/admin/clients/new" if is_new else f"/admin/clients/{client_id}/edit",
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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", _ctx(request, current_user=None, error=None))


@app.post("/login")
def login_action(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Cookie-based login. On success, redirects to `/`."""
    user = authenticate(db, username=username.strip(), password=password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, current_user=None, error="Неверный логин или пароль."),
            status_code=400,
        )
    return login_response(user)


@app.get("/logout")
def logout_action():
    return logout_response()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, current_user=Depends(get_current_user)):
    return templates.TemplateResponse("home.html", _ctx(request, current_user=current_user))


@app.get("/admin/clients", response_class=HTMLResponse)
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
    visits_count = func.coalesce(
        func.sum(case((Visit.is_cancelled.is_(False), 1), else_=0)),
        0,
    )
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

    stmt = (
        select(
            Client.id.label("id"),
            Client.name.label("name"),
            Client.is_confirmed.label("is_confirmed"),
            contact_preview,
            visits_count.label("visits_count"),
        )
        .select_from(Client)
        .join(Visit, Visit.client_id == Client.id, isouter=True)
        .where(*where)
        .group_by(Client.id, Client.name, Client.is_confirmed)
        .order_by(Client.name.asc())
        .limit(500)
    )
    rows = list(db.execute(stmt).mappings().all())
    created_ok = None
    if created is not None:
        created_ok = db.get(Client, created)
    updated_ok = None
    if updated is not None:
        updated_ok = db.get(Client, updated)
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


@app.get("/admin/clients/new", response_class=HTMLResponse)
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


@app.post("/admin/clients/new")
async def admin_client_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form_raw = await request.form()
    form = {k: form_raw.get(k) for k in form_raw.keys()}
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

    client = Client(
        name=name[:200],
        phone=strip_or_none(phone, 30),
        telegram=strip_or_none(telegram, 100),
        vk=strip_or_none(vk, 120),
        instagram=strip_or_none(instagram, 120),
        other_contact=strip_or_none(other_contact, 200),
        age_group=age_group,
        source=source_parsed,
        source_other=strip_or_none(source_other, 200),
        comment=strip_or_none(comment) or None,
        is_confirmed=False if mark_draft == "1" else True,
        birth_day=birth_day,
        birth_month=birth_month,
        birth_year=birth_year,
        created_by_label=format_created_by_label(current_user),
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return RedirectResponse(url=f"/admin/clients?created={client.id}", status_code=303)


@app.get("/admin/clients/{client_id}/edit", response_class=HTMLResponse)
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


@app.post("/admin/clients/{client_id}/edit")
async def admin_client_edit_post(
    request: Request,
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    form_raw = await request.form()
    form: dict[str, str] = {}
    for k in form_raw.keys():
        if k == "is_confirmed":
            continue
        form[k] = str(form_raw.get(k) or "")
    form["is_confirmed"] = "1" if "1" in map(str, form_raw.getlist("is_confirmed")) else "0"

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

    db.commit()
    return RedirectResponse(url=f"/admin/clients?updated={client.id}", status_code=303)


@app.get("/admin/clients/{client_id}", response_class=HTMLResponse)
def admin_client_detail(
    request: Request,
    client_id: int,
    confirmed: str | None = None,
    current_user: AuthUser = Depends(
        require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)
    ),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    visits_stmt = (
        select(Visit)
        .where(Visit.client_id == client_id)
        .options(selectinload(Visit.services))
        .order_by(Visit.performed_date.desc())
    )
    visits = list(db.scalars(visits_stmt).all())
    visit_rows = [
        {
            "visit": v,
            "services_line": visit_services_catalog_line(v),
        }
        for v in visits
    ]

    kit_stmt = (
        select(VisitKitUsage)
        .join(Visit, VisitKitUsage.visit_id == Visit.id)
        .where(Visit.client_id == client_id)
        .options(selectinload(VisitKitUsage.kit), selectinload(VisitKitUsage.visit))
        .order_by(Visit.performed_date.desc(), VisitKitUsage.id.asc())
    )
    kit_rows = list(db.scalars(kit_stmt).all())

    show_admin_actions = current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER)
    return templates.TemplateResponse(
        "admin_client_detail.html",
        _ctx(
            request,
            current_user=current_user,
            client=client,
            visit_rows=visit_rows,
            kit_rows=kit_rows,
            birth_display=format_client_birth_display(
                client.birth_day, client.birth_month, client.birth_year
            ),
            age_group_label=client_age_group_label(client.age_group),
            show_admin_actions=show_admin_actions,
            confirmed_banner=confirmed == "1",
        ),
    )


@app.post("/admin/clients/{client_id}/confirm")
def admin_client_confirm(
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    client.is_confirmed = True
    db.commit()
    return RedirectResponse(url=f"/admin/clients/{client_id}?confirmed=1", status_code=303)


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
        stmt = (
            select(Client)
            .where(Client.name.ilike(f"%{needle}%"))
            .order_by(Client.name.asc())
            .limit(30)
        )
    rows = list(db.scalars(stmt).all())
    clients: list[dict[str, str | int | bool]] = []
    for c in rows:
        parts: list[str] = []
        if c.phone:
            parts.append(c.phone)
        if c.telegram:
            parts.append(f"TG {c.telegram}")
        if c.vk:
            parts.append(f"VK {c.vk}")
        if c.instagram:
            parts.append(f"IG {c.instagram}")
        if c.other_contact:
            parts.append((c.other_contact or "")[:48])
        hint = " · ".join(parts) if parts else "без контакта"
        clients.append(
            {
                "id": c.id,
                "name": c.name,
                "hint": hint,
                "is_draft": not c.is_confirmed,
            }
        )
    return clients


def _kit_stock_label_from_form(db: Session, form_map: dict[str, str], field: str) -> str | None:
    raw = (form_map.get(field) or "").strip()
    if not raw.isdigit():
        return None
    k = db.get(Kit, int(raw))
    if not k:
        return None
    return f"{k.sku} — {k.title} (остаток {k.pieces_available})"


def _kit_reserve_hint_from_form(db: Session, form_map: dict[str, str], field: str) -> str | None:
    raw = (form_map.get(field) or "").strip()
    if not raw.isdigit():
        return None
    return kit_reserve_hint_by_id(db, int(raw))


def _kit_reserve_redirect_base(kit_id: int, form: Any) -> str:
    ar = str(form.get("after_reserve") or "list").strip()
    if ar == "detail":
        return f"/admin/kits/{kit_id}"
    return "/admin/kits"


def _staff_users_for_reserve(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(
                User.is_active.is_(True),
                User.role.in_((UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
            )
            .order_by(User.display_name.asc())
        ).all()
    )


def _kit_reservation_tooltip(kit: Kit, db: Session) -> str:
    if not kit.reserved_at:
        return ""
    parts: list[str] = []
    if kit.reserved_for_client:
        parts.append(f"Клиент: {kit.reserved_for_client.name}")
    if kit.reserved_for_user:
        parts.append(f"Сотрудник: {kit.reserved_for_user.display_name}")
    if kit.reserved_by_user:
        parts.append(f"Забронировал: {kit.reserved_by_user.display_name}")
    tz = get_display_timezone(db)
    when = format_naive_utc_datetime(kit.reserved_at, tz)
    parts.append(f"Когда: {when} ({timezone_label(tz)})")
    return " · ".join(parts)


@app.get("/master/clients/suggest")
def master_clients_suggest(
    q: str = "",
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    return JSONResponse({"clients": _client_suggest_items(db, q)})


@app.get("/master/kits/suggest")
def master_kits_suggest(
    q: str = "",
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    return JSONResponse({"kits": suggest_kits_for_stock(db, q)})


@app.get("/admin/clients/suggest")
def admin_clients_suggest(
    q: str = "",
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    return JSONResponse({"clients": _client_suggest_items(db, q)})


@app.get("/master/visit/new", response_class=HTMLResponse)
def master_visit_new_get(
    request: Request,
    saved: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    service_catalog = list_kit_inlay_services_catalog(db)
    saved_draft_client = False
    if saved and saved.isdigit():
        vid = int(saved)
        v = db.scalar(select(Visit).where(Visit.id == vid).options(selectinload(Visit.client)))
        if v and v.client and not v.client.is_confirmed:
            saved_draft_client = True
    return templates.TemplateResponse(
        "master_visit_kit_inlay.html",
        _ctx(
            request,
            current_user=current_user,
            service_catalog=service_catalog,
            stock_kit_selected_label=None,
            stock_kit_reserve_hint=None,
            extra_stock_kit_selected_label=None,
            extra_stock_kit_reserve_hint=None,
            default_date=date.today().isoformat(),
            form_prefill={},
            selected_client=None,
            error=None,
            saved=saved,
            saved_draft_client=saved_draft_client,
        ),
    )


@app.post("/master/visit/new")
async def master_visit_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    inp = parse_kit_inlay_form(form)
    try:
        visit = save_kit_inlay_visit(
            db,
            current_user.id,
            inp,
            created_by_label=format_created_by_label(current_user),
        )
    except ValueError as exc:
        service_catalog = list_kit_inlay_services_catalog(db)
        form_map = _form_to_str_map(form)
        selected_client = None
        eid = (form_map.get("existing_client_id") or "").strip()
        if eid.isdigit():
            selected_client = db.get(Client, int(eid))
        performed = (form_map.get("performed_date") or "").strip() or date.today().isoformat()
        return templates.TemplateResponse(
            "master_visit_kit_inlay.html",
            _ctx(
                request,
                current_user=current_user,
                service_catalog=service_catalog,
                stock_kit_selected_label=_kit_stock_label_from_form(db, form_map, "stock_kit_id"),
                stock_kit_reserve_hint=_kit_reserve_hint_from_form(db, form_map, "stock_kit_id"),
                extra_stock_kit_selected_label=_kit_stock_label_from_form(
                    db, form_map, "own_extra_stock_kit_id"
                ),
                extra_stock_kit_reserve_hint=_kit_reserve_hint_from_form(
                    db, form_map, "own_extra_stock_kit_id"
                ),
                default_date=performed,
                form_prefill=form_map,
                selected_client=selected_client,
                error=str(exc),
                saved=None,
                saved_draft_client=False,
            ),
            status_code=400,
        )
    return RedirectResponse(url=f"/master/visit/new?saved={visit.id}", status_code=303)


@app.get("/admin/kits", response_class=HTMLResponse)
def admin_kits_list(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
    db: Session = Depends(get_db),
):
    kits = list(
        db.scalars(
            select(Kit)
            .options(
                selectinload(Kit.reserved_by_user),
                selectinload(Kit.reserved_for_client),
                selectinload(Kit.reserved_for_user),
            )
            .order_by(Kit.sku.asc())
        ).all()
    )
    staff_users = _staff_users_for_reserve(db)
    kit_rows = [{"kit": k, "reserve_tooltip": _kit_reservation_tooltip(k, db)} for k in kits]
    return templates.TemplateResponse(
        "admin_kits.html",
        _ctx(
            request,
            current_user=current_user,
            kit_rows=kit_rows,
            staff_users=staff_users,
            msg=msg,
            err=err,
        ),
    )


@app.get("/admin/kits/new", response_class=HTMLResponse)
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
            fp={},
            form_action="/admin/kits/new",
            error=None,
        ),
    )


@app.post("/admin/kits/new")
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
        db.add(kit)
        db.commit()
        return RedirectResponse(url=f"/admin/kits/{kit.id}?msg=created", status_code=303)
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin_kit_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=True,
                kit=None,
                fp=kit_new_error_prefill(form),
                form_action="/admin/kits/new",
                error=str(exc),
            ),
            status_code=400,
        )


@app.get("/admin/kits/{kit_id}", response_class=HTMLResponse)
def admin_kit_detail(
    request: Request,
    kit_id: int,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
    db: Session = Depends(get_db),
):
    kit = db.scalar(
        select(Kit)
        .options(
            selectinload(Kit.reserved_by_user),
            selectinload(Kit.reserved_for_client),
            selectinload(Kit.reserved_for_user),
        )
        .where(Kit.id == kit_id)
    )
    if not kit:
        raise HTTPException(status_code=404, detail="Комплект не найден")
    return templates.TemplateResponse(
        "admin_kit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            kit=kit,
            reserve_tooltip=_kit_reservation_tooltip(kit, db),
            staff_users=_staff_users_for_reserve(db),
            msg=msg,
            err=err,
        ),
    )


@app.get("/admin/kits/{kit_id}/edit", response_class=HTMLResponse)
def admin_kit_edit_get(
    request: Request,
    kit_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.get(Kit, kit_id)
    if not kit:
        raise HTTPException(status_code=404, detail="Комплект не найден")
    return templates.TemplateResponse(
        "admin_kit_form.html",
        _ctx(
            request,
            current_user=current_user,
            is_new=False,
            kit=kit,
            fp=kit_to_form_prefill(kit),
            form_action=f"/admin/kits/{kit_id}/edit",
            error=None,
        ),
    )


@app.post("/admin/kits/{kit_id}/edit")
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
        d = parse_kit_admin_form(form, for_create=False)
        validate_kit_admin_form(d, for_create=False)
        if d.sku != kit.sku:
            oid = db.scalar(select(Kit.id).where(Kit.sku == d.sku, Kit.id != kit.id))
            if oid:
                raise ValueError("Комплект с таким артикулом уже есть")
        apply_kit_admin_form(kit, d)
        db.commit()
        return RedirectResponse(url=f"/admin/kits/{kit_id}?msg=saved", status_code=303)
    except ValueError as exc:
        fp = kit_edit_error_prefill(form)
        return templates.TemplateResponse(
            "admin_kit_form.html",
            _ctx(
                request,
                current_user=current_user,
                is_new=False,
                kit=kit,
                fp=fp,
                form_action=f"/admin/kits/{kit_id}/edit",
                error=str(exc),
            ),
            status_code=400,
        )


@app.post("/admin/kits/{kit_id}/reserve")
async def admin_kit_reserve_post(
    kit_id: int,
    request: Request,
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
    db: Session = Depends(get_db),
):
    kit = db.get(Kit, kit_id)
    if not kit:
        return RedirectResponse(
            url="/admin/kits?err=" + quote("Комплект не найден", safe=""),
            status_code=303,
        )

    form = await request.form()
    redirect_base = _kit_reserve_redirect_base(kit_id, form)
    action = str(form.get("action") or "").strip().lower()

    if action == "clear":
        if current_user.role == UserRole.MASTER:
            if not kit.is_reserved or kit.reserved_by_user_id != current_user.id:
                return RedirectResponse(
                    url=redirect_base
                    + "?err="
                    + quote(
                        "Снять резерв может автор резерва или администратор.",
                        safe="",
                    ),
                    status_code=303,
                )
        kit.reserved_at = None
        kit.reserved_by_user_id = None
        kit.reserved_for_client_id = None
        kit.reserved_for_user_id = None
        db.commit()
        return RedirectResponse(url=redirect_base + "?msg=cleared", status_code=303)

    if current_user.role == UserRole.MASTER:
        if kit.is_reserved and kit.reserved_by_user_id != current_user.id:
            return RedirectResponse(
                url=redirect_base
                + "?err="
                + quote(
                    "Этот комплект зарезервирован другим пользователем. Изменить резерв может только администратор.",
                    safe="",
                ),
                status_code=303,
            )

    cid_raw = str(form.get("reserved_for_client_id") or "").strip()
    uid_raw = str(form.get("reserved_for_user_id") or "").strip()
    cid = int(cid_raw) if cid_raw.isdigit() else None
    uid = int(uid_raw) if uid_raw.isdigit() else None
    if cid is None and uid is None:
        return RedirectResponse(
            url=redirect_base
            + "?err="
            + quote("Укажите клиента и/или сотрудника для резерва.", safe=""),
            status_code=303,
        )
    if cid is not None:
        if not db.get(Client, cid):
            return RedirectResponse(
                url=redirect_base + "?err=" + quote("Клиент не найден.", safe=""),
                status_code=303,
            )
    if uid is not None:
        u = db.get(User, uid)
        if not u or u.role not in (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
            return RedirectResponse(
                url=redirect_base + "?err=" + quote("Сотрудник не найден.", safe=""),
                status_code=303,
            )

    kit.reserved_at = datetime.utcnow()
    kit.reserved_by_user_id = current_user.id
    kit.reserved_for_client_id = cid
    kit.reserved_for_user_id = uid
    db.commit()
    return RedirectResponse(url=redirect_base + "?msg=reserved", status_code=303)


@app.get("/admin/visits", response_class=HTMLResponse)
def admin_visits(
    request: Request,
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Visit)
        .options(selectinload(Visit.client), selectinload(Visit.services))
        .order_by(Visit.performed_date.desc())
        .limit(200)
    )
    visits = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "admin_visits.html",
        _ctx(request, current_user=current_user, visits=visits),
    )


@app.get("/admin/visits/{visit_id}", response_class=HTMLResponse)
def admin_visit_detail(
    visit_id: int,
    request: Request,
    client_err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    visit = db.scalar(
        select(Visit)
        .options(
            selectinload(Visit.client),
            selectinload(Visit.services),
            selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit),
            selectinload(Visit.masters).selectinload(VisitMaster.master),
        )
        .where(Visit.id == visit_id)
    )
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")

    service_displays = [build_service_human_display(vs) for vs in visit.services]

    mix_bonus_master_label: str | None = None
    if visit.mix_bonus_master_id:
        u = db.get(User, visit.mix_bonus_master_id)
        if u and (u.display_name or "").strip():
            mix_bonus_master_label = u.display_name.strip()
        else:
            mix_bonus_master_label = f"ID {visit.mix_bonus_master_id}"

    visit_creator_label: str | None = None
    if visit.created_by_user_id:
        cu = db.get(User, visit.created_by_user_id)
        if cu and (cu.display_name or "").strip():
            visit_creator_label = cu.display_name.strip()

    duration_h = visit.duration_minutes // 60
    duration_m = visit.duration_minutes % 60

    return templates.TemplateResponse(
        "admin_visit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            visit=visit,
            service_displays=service_displays,
            mix_bonus_master_label=mix_bonus_master_label,
            price_type_ru=ru_price_type(visit.price_type),
            mix_source_ru=ru_mix_source(visit.mix_source),
            mix_complexity_ru=ru_mix_complexity(getattr(visit, "mix_complexity", None)),
            materials_used_ru="Да" if (visit.kanekalon_grams > 0 or visit.kudri_grams > 0) else "Нет",
            kit_usages_note=kit_usages_empty_explanation(),
            visit_creator_label=visit_creator_label,
            duration_h=duration_h,
            duration_m=duration_m,
            client_err=client_err,
        ),
    )


@app.post("/admin/visits/{visit_id}/client")
async def admin_visit_change_client(
    visit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER, UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)
    if visit is None:
        raise HTTPException(status_code=404, detail="Визит не найден")

    policy = visit_client_change_policy(visit, current_user, db)
    if not policy.can_change:
        raise HTTPException(status_code=403, detail=policy.message_when_blocked or "Недостаточно прав")

    form = await request.form()
    raw = form.get("new_client_id")
    try:
        new_cid = int(str(raw).strip())
    except (TypeError, ValueError):
        return RedirectResponse(url=f"/admin/visits/{visit_id}?client_err=bad_id", status_code=303)

    confirm_late = str(form.get("confirm_late") or "").lower() in ("1", "on", "true", "yes")
    if policy.super_outside_window and not confirm_late:
        return RedirectResponse(url=f"/admin/visits/{visit_id}?client_err=need_confirm", status_code=303)

    if new_cid == visit.client_id:
        return RedirectResponse(url=f"/admin/visits/{visit_id}?client_err=same", status_code=303)

    new_client = db.get(Client, new_cid)
    if new_client is None:
        return RedirectResponse(url=f"/admin/visits/{visit_id}?client_err=not_found", status_code=303)

    old_id = visit.client_id
    visit.client_id = new_cid
    visit.client_age_group = new_client.age_group
    visit.updated_at = datetime.utcnow()
    visit.updated_by_user_id = current_user.id
    db.add(
        VisitAuditLog(
            visit_id=visit.id,
            changed_by_user_id=current_user.id,
            field_name="client_id",
            old_value=str(old_id),
            new_value=str(new_cid),
        )
    )
    db.commit()
    return RedirectResponse(url=f"/admin/visits/{visit_id}", status_code=303)


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(
    request: Request,
    saved: int | None = None,
    current_user=Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    salon = db.get(Setting, "salon_cut_pct")
    salon_cut_pct = salon.value if salon else "0.3"
    edit_days = db.get(Setting, "edit_window_days")
    edit_window_days = edit_days.value if edit_days else "2"
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    kanek_per_100 = str((pk.price_per_gram * 100) if pk else 400.0)
    kudri_per_100 = str((pku.price_per_gram * 100) if pku else 800.0)
    display_tz = get_display_timezone(db)
    return templates.TemplateResponse(
        "admin_settings.html",
        _ctx(
            request,
            current_user=current_user,
            salon_cut_pct=salon_cut_pct,
            edit_window_days=edit_window_days,
            kanek_per_100g=kanek_per_100,
            kudri_per_100g=kudri_per_100,
            display_timezone=display_tz,
            timezone_choices=ALLOWED_TIMEZONES,
            saved=bool(saved),
        ),
    )


@app.post("/admin/settings")
def admin_settings_save(
    salon_cut_pct: str = Form(...),
    edit_window_days: str = Form(...),
    kanek_per_100g: str = Form(...),
    kudri_per_100g: str = Form(...),
    display_timezone: str = Form(...),
    current_user=Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    tz_raw = display_timezone.strip()
    if tz_raw not in ALLOWED_TIMEZONE_IDS:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)

    value = salon_cut_pct.strip().replace(",", ".")
    try:
        pct = float(value)
    except ValueError:
        pct = -1
    if pct < 0 or pct > 1:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)

    row = db.get(Setting, "salon_cut_pct")
    if not row:
        row = Setting(key="salon_cut_pct", value=str(pct))
        db.add(row)
    else:
        row.value = str(pct)

    try:
        days = int(edit_window_days.strip())
    except ValueError:
        days = -1
    if days < 0 or days > 365:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)
    drow = db.get(Setting, "edit_window_days")
    if not drow:
        db.add(Setting(key="edit_window_days", value=str(days)))
    else:
        drow.value = str(days)

    now = datetime.utcnow()
    try:
        k100 = float(kanek_per_100g.strip().replace(",", "."))
        ku100 = float(kudri_per_100g.strip().replace(",", "."))
    except ValueError:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)
    if k100 < 0 or ku100 < 0:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)

    for mt, per100 in ((MaterialType.KANEKALON, k100), (MaterialType.KUDRI, ku100)):
        per_g = per100 / 100.0
        mrow = db.get(MaterialPriceCurrent, mt)
        if not mrow:
            db.add(MaterialPriceCurrent(material_type=mt, price_per_gram=per_g, updated_at=now))
    else:
        mrow.price_per_gram = per_g
        mrow.updated_at = now

    tz_row = db.get(Setting, "display_timezone")
    if not tz_row:
        db.add(Setting(key="display_timezone", value=tz_raw))
    else:
        tz_row.value = tz_raw

    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)

