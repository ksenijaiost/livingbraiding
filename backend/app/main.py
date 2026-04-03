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

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, authenticate, get_current_user, login_response, logout_response, require_role
from app.client_validation import (
    CLIENT_AGE_GROUP_OPTIONS,
    client_has_any_contact,
    format_created_by_label,
    load_client_source_options,
    parse_age_group,
    parse_birth_fields,
    parse_client_source,
    strip_or_none,
)
from app.db.models import (
    Client,
    MaterialPriceCurrent,
    MaterialType,
    Setting,
    UserRole,
    Visit,
    VisitKitUsage,
    VisitMaster,
)
from app.db.session import get_db
from app.demo_kit_inlay_visit import (
    list_kit_inlay_services,
    list_kits_for_stock,
    parse_kit_inlay_form,
    save_kit_inlay_visit,
)
from app.seed import ensure_seed_data
from app.ui_visit_display import (
    build_service_human_display,
    kit_usages_empty_explanation,
    ru_client_type,
    ru_mix_complexity,
    ru_mix_source,
    ru_price_type,
)

app = FastAPI(title="livingbraiding")
templates = Jinja2Templates(directory="app/templates")


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
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    """Admin: client list — id, name, contact preview, visit count (non-cancelled only)."""
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
    return templates.TemplateResponse(
        "admin_clients.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            q=q_norm,
            created_ok=created_ok,
        ),
    )


@app.get("/admin/clients/new", response_class=HTMLResponse)
def admin_client_new_get(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
):
    return templates.TemplateResponse(
        "admin_client_new.html",
        _ctx(
            request,
            current_user=current_user,
            age_options=CLIENT_AGE_GROUP_OPTIONS,
            source_options=load_client_source_options(),
            error=None,
            form={},
        ),
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
        return templates.TemplateResponse(
            "admin_client_new.html",
            _ctx(
                request,
                current_user=current_user,
                age_options=CLIENT_AGE_GROUP_OPTIONS,
                source_options=load_client_source_options(),
                error=err,
                form=form,
            ),
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


@app.get("/master/visit/new", response_class=HTMLResponse)
def master_visit_new_get(
    request: Request,
    saved: str | None = None,
    current_user=Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    services = list_kit_inlay_services(db)
    kits = list_kits_for_stock(db)
    return templates.TemplateResponse(
        "master_visit_kit_inlay.html",
        _ctx(
            request,
            current_user=current_user,
            services=services,
            kits=kits,
            default_date=date.today().isoformat(),
            form_prefill={},
            error=None,
            saved=saved,
        ),
    )


@app.post("/master/visit/new")
async def master_visit_new_post(
    request: Request,
    current_user=Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    inp = parse_kit_inlay_form(form)
    try:
        visit = save_kit_inlay_visit(db, current_user.id, inp)
    except ValueError as exc:
        services = list_kit_inlay_services(db)
        kits = list_kits_for_stock(db)
        return templates.TemplateResponse(
            "master_visit_kit_inlay.html",
            _ctx(
                request,
                current_user=current_user,
                services=services,
                kits=kits,
                default_date=date.today().isoformat(),
                form_prefill={},
                error=str(exc),
                saved=None,
            ),
            status_code=400,
        )
    return RedirectResponse(url=f"/master/visit/new?saved={visit.id}", status_code=303)


@app.get("/admin/visits", response_class=HTMLResponse)
def admin_visits(
    request: Request,
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
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
    current_user=Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
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

    return templates.TemplateResponse(
        "admin_visit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            visit=visit,
            service_displays=service_displays,
            client_type_ru=ru_client_type(visit.client_type),
            price_type_ru=ru_price_type(visit.price_type),
            mix_source_ru=ru_mix_source(visit.mix_source),
            mix_complexity_ru=ru_mix_complexity(getattr(visit, "mix_complexity", None)),
            materials_used_ru="Да" if (visit.kanekalon_grams > 0 or visit.kudri_grams > 0) else "Нет",
            kit_usages_note=kit_usages_empty_explanation(),
        ),
    )


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
    return templates.TemplateResponse(
        "admin_settings.html",
        _ctx(
            request,
            current_user=current_user,
            salon_cut_pct=salon_cut_pct,
            edit_window_days=edit_window_days,
            kanek_per_100g=kanek_per_100,
            kudri_per_100g=kudri_per_100,
            saved=bool(saved),
        ),
    )


@app.post("/admin/settings")
def admin_settings_save(
    salon_cut_pct: str = Form(...),
    edit_window_days: str = Form(...),
    kanek_per_100g: str = Form(...),
    kudri_per_100g: str = Form(...),
    current_user=Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
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

    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)

