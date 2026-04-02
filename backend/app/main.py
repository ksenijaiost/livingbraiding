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
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import authenticate, get_current_user, login_response, logout_response, require_role
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
    current_user=Depends(require_role(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    """
    Admin: client list with basic aggregates.

    Notes:
    - spent_total uses `Visit.amount_from_client` (raw money received).
    - earned_total uses `Visit.profit_before_split` which should already include all deductions.
    """
    q_norm = (q or "").strip()
    where = []
    if q_norm:
        where.append(or_(Client.name.ilike(f"%{q_norm}%"), Client.contact.ilike(f"%{q_norm}%")))

    visits_count = func.count(Visit.id)
    spent_total = func.coalesce(func.sum(Visit.amount_from_client), 0.0)
    earned_total = func.coalesce(func.sum(Visit.profit_before_split), 0.0)

    stmt = (
        select(
            Client.id.label("id"),
            Client.name.label("name"),
            Client.contact.label("contact"),
            visits_count.label("visits_count"),
            spent_total.label("spent_total"),
            earned_total.label("earned_total"),
        )
        .select_from(Client)
        .join(Visit, Visit.client_id == Client.id, isouter=True)
        .where(*where)
        .group_by(Client.id)
        .order_by(Client.name.asc())
        .limit(500)
    )
    rows = list(db.execute(stmt).mappings().all())
    return templates.TemplateResponse(
        "admin_clients.html",
        _ctx(request, current_user=current_user, rows=rows, q=q_norm),
    )


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
    current_user=Depends(require_role(UserRole.ADMIN)),
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
    current_user=Depends(require_role(UserRole.ADMIN)),
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
            materials_used_ru="Да" if visit.materials_used else "Нет",
            kit_usages_note=kit_usages_empty_explanation(),
        ),
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(
    request: Request,
    saved: int | None = None,
    current_user=Depends(require_role(UserRole.ADMIN)),
    db: Session = Depends(get_db),
):
    salon = db.get(Setting, "salon_cut_pct")
    salon_cut_pct = salon.value if salon else "0.3"
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
            kanek_per_100g=kanek_per_100,
            kudri_per_100g=kudri_per_100,
            saved=bool(saved),
        ),
    )


@app.post("/admin/settings")
def admin_settings_save(
    salon_cut_pct: str = Form(...),
    kanek_per_100g: str = Form(...),
    kudri_per_100g: str = Form(...),
    current_user=Depends(require_role(UserRole.ADMIN)),
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

