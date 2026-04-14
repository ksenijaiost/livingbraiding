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

import json
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.admin_questionnaire_fields import router as admin_questionnaire_fields_router
from app.admin_service_catalog import router as admin_service_catalog_router
from app.auth import (
    AuthUser,
    authenticate,
    get_current_user,
    issue_session_cookie,
    login_response,
    logout_response,
    require_role,
)
from app.ru_labels import (
    format_price_integer_rub,
    ru_master_level,
    ru_questionnaire_field_type,
    ru_user_role,
    ru_user_roles_payout_suffix,
)
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
    ClientAuditLog,
    ClientThermoTemplate,
    Kit,
    KitAuditLog,
    KitAuthorStaff,
    MaterialPriceCurrent,
    MaterialType,
    PayrollFundPayoutPaymentKind,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    SettingAuditLog,
    User,
    UserRole,
    Visit,
    VisitAuditLog,
    VisitKitUsage,
    VisitMaster,
    WorkRate,
    WorkRateAuditLog,
)
from app.audit import diff_fields, write_audit_rows
from app.operational_report import (
    build_operational_report,
    list_closed_payroll_periods,
    result_to_template_dict,
)
from app.payroll_fund import (
    employee_fund_balance,
    employee_payout_total_net,
    ledger_balances,
    post_payout,
    recent_ledger_rows,
    storno_source_accruals,
    studio_fund_balance,
)
from app.db.session import get_db
from app.kit_crud import (
    apply_kit_admin_form,
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
from app import admin_studio_expenses as admin_studio_expenses_routes
from app import product_sales as product_sales_routes
from app import work_products as work_products_routes
from app.kit_inlay_visit import (
    collect_questionnaire_prefill_from_form,
    get_salon_cut_pct,
    kit_reserve_hint_by_id,
    list_master_visit_services_catalog,
    master_visit_step1_prefill_from_form,
    parse_kit_inlay_form,
    save_kit_inlay_visit,
    suggest_kits_for_stock,
    validate_master_visit_step1,
)
from app.seed import ensure_seed_data
from app.thermo_visit import (
    collect_thermo_prefill_from_form,
    list_client_thermo_templates_for_visit,
)
from app.user_roles import (
    get_roles_for_user,
    select_users_with_any_role,
    select_users_with_role,
    user_has_any_role,
)
from app.visit_edit_policy import visit_client_change_policy
from app.visit_edit_policy import is_in_closed_payroll_period
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
app.include_router(admin_studio_expenses_routes.router)
app.include_router(product_sales_routes.router)
app.include_router(work_products_routes.router)
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_master_level"] = ru_master_level
templates.env.globals["ru_questionnaire_field_type"] = ru_questionnaire_field_type
templates.env.globals["ru_user_role"] = ru_user_role
templates.env.globals["format_price_integer_rub"] = format_price_integer_rub


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
    return login_response(user, db)


@app.post("/session/active-role")
def session_set_active_role(
    request: Request,
    role: str = Form(...),
    current_user: AuthUser = Depends(get_current_user),
):
    try:
        new_role = UserRole(role.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректная роль")
    if new_role not in current_user.roles:
        raise HTTPException(status_code=403, detail="Эта роль не назначена пользователю")
    loc = request.headers.get("referer") or "/"
    resp = RedirectResponse(url=loc, status_code=303)
    issue_session_cookie(resp, current_user.id, new_role)
    return resp


@app.get("/logout")
def logout_action():
    return logout_response()


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payroll_home: dict[str, Any] | None = None
    if current_user.role in (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
        show_studio = UserRole.ADMIN_SUPER in current_user.roles
        payroll_home = {
            "personal_balance": employee_fund_balance(db, current_user.id),
            "paid_net": employee_payout_total_net(db, current_user.id),
            "show_studio": show_studio,
            "studio_balance": studio_fund_balance(db) if show_studio else None,
        }
    return templates.TemplateResponse(
        "home.html",
        _ctx(request, current_user=current_user, payroll_home=payroll_home),
    )


@app.get("/service-catalog", response_class=HTMLResponse)
def service_catalog_view(
    request: Request,
    category_id: int | None = None,
    subcategory_id: int | None = None,
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
    db: Session = Depends(get_db),
):
    """
    Просмотр прайса: категория → подкатегория → таблица услуг.
    Пока все диапазоны цен (младший / мастер / старший) без фильтра по уровню текущего пользователя.
    """
    if category_id is not None and category_id <= 0:
        category_id = None
    if subcategory_id is not None and subcategory_id <= 0:
        subcategory_id = None

    # Только категории из формы визита; «Продажа материала», «Заказ» и т.п. — в прайсе «Товары» (/price/products).
    categories = list(
        db.scalars(
            select(ServiceCategory)
            .where(
                ServiceCategory.is_active.is_(True),
                ServiceCategory.include_in_visit.is_(True),
            )
            .order_by(ServiceCategory.name.asc())
        ).all()
    )

    selected_category: ServiceCategory | None = None
    subcategories: list[ServiceSubcategory] = []
    selected_subcategory: ServiceSubcategory | None = None
    services: list[Service] = []
    mismatch = False

    sub_from_q: ServiceSubcategory | None = None
    if subcategory_id is not None and subcategory_id > 0:
        sub_from_q = db.scalar(
            select(ServiceSubcategory)
            .options(selectinload(ServiceSubcategory.category))
            .where(ServiceSubcategory.id == subcategory_id, ServiceSubcategory.is_active.is_(True))
        )

    if (
        sub_from_q
        and sub_from_q.category
        and sub_from_q.category.is_active
        and sub_from_q.category.include_in_visit
    ):
        if category_id is not None and category_id > 0 and category_id != sub_from_q.category_id:
            mismatch = True
            selected_category = db.scalar(
                select(ServiceCategory).where(
                    ServiceCategory.id == category_id,
                    ServiceCategory.is_active.is_(True),
                    ServiceCategory.include_in_visit.is_(True),
                )
            )
            if selected_category:
                subcategories = list(
                    db.scalars(
                        select(ServiceSubcategory)
                        .where(
                            ServiceSubcategory.category_id == category_id,
                            ServiceSubcategory.is_active.is_(True),
                        )
                        .order_by(ServiceSubcategory.name.asc())
                    ).all()
                )
        else:
            selected_subcategory = sub_from_q
            selected_category = sub_from_q.category
            subcategories = list(
                db.scalars(
                    select(ServiceSubcategory)
                    .where(
                        ServiceSubcategory.category_id == selected_category.id,
                        ServiceSubcategory.is_active.is_(True),
                    )
                    .order_by(ServiceSubcategory.name.asc())
                ).all()
            )
            services = list(
                db.scalars(
                    select(Service)
                    .where(Service.subcategory_id == sub_from_q.id)
                    .order_by(Service.is_active.desc(), Service.name.asc())
                ).all()
            )
    elif category_id is not None and category_id > 0:
        selected_category = db.scalar(
            select(ServiceCategory).where(
                ServiceCategory.id == category_id,
                ServiceCategory.is_active.is_(True),
                ServiceCategory.include_in_visit.is_(True),
            )
        )
        if selected_category:
            subcategories = list(
                db.scalars(
                    select(ServiceSubcategory)
                    .where(
                        ServiceSubcategory.category_id == category_id,
                        ServiceSubcategory.is_active.is_(True),
                    )
                    .order_by(ServiceSubcategory.name.asc())
                ).all()
            )
            if (
                (subcategory_id is None or subcategory_id <= 0)
                and len(subcategories) == 1
            ):
                selected_subcategory = subcategories[0]
                services = list(
                    db.scalars(
                        select(Service)
                        .where(Service.subcategory_id == selected_subcategory.id)
                        .order_by(Service.is_active.desc(), Service.name.asc())
                    ).all()
                )

    return templates.TemplateResponse(
        "service_catalog_view.html",
        _ctx(
            request,
            current_user=current_user,
            title="Прайс · Услуги",
            categories=categories,
            selected_category=selected_category,
            subcategories=subcategories,
            selected_subcategory=selected_subcategory,
            services=services,
            mismatch=mismatch,
        ),
    )


@app.get("/price", response_class=HTMLResponse)
def price_index(
    request: Request,
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
):
    return templates.TemplateResponse(
        "price_index.html",
        _ctx(request, current_user=current_user),
    )


def _format_product_catalog_price(s: Service) -> str | None:
    """Один столбец «цена»: приоритет «мастер», иначе младший/старший, диапазон или одно число."""
    for lo, hi in (
        (s.price_middle_from, s.price_middle_to),
        (s.price_junior_from, s.price_junior_to),
        (s.price_senior_from, s.price_senior_to),
    ):
        if lo is None and hi is None:
            continue
        if lo is not None and hi is not None:
            if abs(float(lo) - float(hi)) < 0.01:
                return format_price_integer_rub(lo)
            return f"{int(round(float(lo)))}–{int(round(float(hi)))} ₽"
        if lo is not None:
            return format_price_integer_rub(lo)
        if hi is not None:
            return format_price_integer_rub(hi)
    return None


@app.get("/price/products", response_class=HTMLResponse)
def products_catalog_view(
    request: Request,
    category: str | None = None,
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
    db: Session = Depends(get_db),
):
    # Позиции из каталога услуг, которые не в форме визита («Продажа материала», «Заказ», …).
    cats = list(
        db.scalars(
            select(ServiceCategory.name)
            .where(
                ServiceCategory.is_active.is_(True),
                ServiceCategory.include_in_visit.is_(False),
            )
            .order_by(ServiceCategory.name.asc())
        ).all()
    )
    selected = (category or "").strip() or (cats[0] if cats else None)
    rows: list[SimpleNamespace] = []
    if selected:
        services = list(
            db.scalars(
                select(Service)
                .options(selectinload(Service.subcategory))
                .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
                .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
                .where(
                    ServiceCategory.name == selected,
                    ServiceCategory.is_active.is_(True),
                    ServiceCategory.include_in_visit.is_(False),
                    ServiceSubcategory.is_active.is_(True),
                    Service.is_active.is_(True),
                )
                .order_by(
                    ServiceSubcategory.name.asc(),
                    Service.is_active.desc(),
                    Service.name.asc(),
                )
            ).all()
        )
        for s in services:
            sub = s.subcategory
            rows.append(
                SimpleNamespace(
                    subcategory_name=sub.name if sub else "—",
                    name=s.name,
                    price=_format_product_catalog_price(s),
                    is_active=s.is_active,
                )
            )
    return templates.TemplateResponse(
        "products_catalog_view.html",
        _ctx(
            request,
            current_user=current_user,
            categories=cats,
            selected_category=selected,
            rows=rows,
        ),
    )


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
    before = SimpleNamespace(**{k: getattr(client, k) for k in (
        "name",
        "phone",
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
    )})
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
    client.updated_at = datetime.utcnow()
    client.updated_by_user_id = current_user.id

    changes = diff_fields(
        before,
        client,
        (
            "name",
            "phone",
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
        .where(Visit.client_id == client_id, Visit.is_cancelled.is_(False))
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
        .where(Visit.client_id == client_id, Visit.is_cancelled.is_(False))
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

    show_admin_actions = current_user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER)
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
    before = SimpleNamespace(is_confirmed=client.is_confirmed)
    client.is_confirmed = True
    client.updated_at = datetime.utcnow()
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
            ".",
            "",
        )
        conds = [Client.name.ilike(f"%{needle}%")]
        if digits:
            conds.append(phone_norm.like(f"%{digits}%"))
        stmt = (
            select(Client)
            .where(or_(*conds))
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
            select_users_with_any_role(
                UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER
            ).order_by(User.display_name.asc())
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
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
    db: Session = Depends(get_db),
):
    return JSONResponse({"clients": _client_suggest_items(db, q)})


@app.get("/master/kits/suggest")
def master_kits_suggest(
    q: str = "",
    current_user: AuthUser = Depends(
        require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    ),
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


@app.get("/master/clients/{client_id}/thermo-templates")
def master_client_thermo_templates(
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    rows = list_client_thermo_templates_for_visit(db, client_id)
    return JSONResponse(
        {
            "templates": [
                {
                    "id": t.id,
                    "label": t.label,
                    "created_at": t.created_at.strftime("%d.%m.%Y %H:%M"),
                }
                for t in rows
            ]
        }
    )


def _masters_for_visit_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_role(UserRole.MASTER).order_by(
                User.display_name.asc(), User.username.asc()
            )
        ).all()
    )


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
):
    performed = (form_prefill.get("performed_date") or "").strip() or (
        default_date or date.today().isoformat()
    )
    salon_cut_pct = get_salon_cut_pct(db)
    # Prices per gram for preview calculation on step 1.
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    material_price_per_gram = {
        "kanekalon": float(pk.price_per_gram) if pk else 0.0,
        "kudri": float(pku.price_per_gram) if pku else 0.0,
    }
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
            extra_stock_kit_selected_label=_kit_stock_label_from_form(
                db, form_prefill, "own_extra_stock_kit_id"
            ),
            extra_stock_kit_reserve_hint=_kit_reserve_hint_from_form(
                db, form_prefill, "own_extra_stock_kit_id"
            ),
            salon_cut_pct=salon_cut_pct,
            material_price_per_gram_json=json.dumps(material_price_per_gram, ensure_ascii=False),
            visit_master_level_ru=ru_master_level(current_user.master_level),
            default_date=performed,
            form_prefill=form_prefill,
            selected_client=selected_client,
            error=error,
            saved=saved,
            saved_draft_client=saved_draft_client,
        ),
        status_code=status_code,
    )


@app.get("/master/visit/new", response_class=HTMLResponse)
def master_visit_new_get(
    request: Request,
    saved: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    saved_draft_client = False
    if saved and saved.isdigit():
        vid = int(saved)
        v = db.scalar(select(Visit).where(Visit.id == vid).options(selectinload(Visit.client)))
        if v and v.client and not v.client.is_confirmed:
            saved_draft_client = True
    return _master_visit_step1_template_response(
        request,
        current_user=current_user,
        db=db,
        form_prefill={},
        visit_master_on_ids=[current_user.id],
        visit_master_pct_str={},
        saved=saved,
        saved_draft_client=saved_draft_client,
        default_date=date.today().isoformat(),
    )


@app.post("/master/visit/new")
async def master_visit_new_post(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        inp = parse_kit_inlay_form(form, single_master_default_id=current_user.id)
        visit = save_kit_inlay_visit(
            db,
            current_user.id,
            inp,
            created_by_label=format_created_by_label(current_user),
        )
    except ValueError as exc:
        fp, vm_on_ids, vm_pct_str = master_visit_step1_prefill_from_form(form)
        fp.update(collect_questionnaire_prefill_from_form(form))
        fp.update(collect_thermo_prefill_from_form(form))
        selected_client = None
        eid = (fp.get("existing_client_id") or "").strip()
        if eid.isdigit():
            selected_client = db.get(Client, int(eid))
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
            fp={"kit_author_ids": [], "discount_percent": "0"},
            form_action="/admin/kits/new",
            error=None,
            staff_for_kit_authors=list_masters_for_kit_author_pick(db),
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
        db.flush()
        sync_kit_authors(db, kit, form)
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
                staff_for_kit_authors=list_masters_for_kit_author_pick(db),
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
    return templates.TemplateResponse(
        "admin_kit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            kit=kit,
            audit_rows=audit_rows,
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
    kit = db.scalar(
        select(Kit)
        .options(selectinload(Kit.author_staff_links))
        .where(Kit.id == kit_id)
    )
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
            staff_for_kit_authors=list_masters_for_kit_author_pick(db),
        ),
    )


@app.post("/admin/kits/{kit_id}/discount")
async def admin_kit_discount_post(
    kit_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    kit = db.get(Kit, kit_id)
    if not kit:
        return RedirectResponse(
            url="/admin/kits?err=" + quote("Комплект не найден", safe=""),
            status_code=303,
        )
    form = await request.form()
    try:
        discount = parse_discount_percent_from_form(form)
    except ValueError as exc:
        err_q = quote(str(exc), safe="")
        red = str(form.get("redirect_to") or "").strip().lower()
        if red == "detail":
            return RedirectResponse(url=f"/admin/kits/{kit_id}?err={err_q}", status_code=303)
        return RedirectResponse(url="/admin/kits?err=" + err_q, status_code=303)
    price = float(kit.stock_price_total or 0.0)
    red = str(form.get("redirect_to") or "").strip().lower()
    err_no_cost = (
        "Чтобы задать скидку, сначала укажите себестоимость комплекта в карточке (редактирование)."
    )
    ct = kit.cost_total
    if ct is None or float(ct) <= 0:
        if discount > 0:
            err_q = quote(err_no_cost, safe="")
            if red == "detail":
                return RedirectResponse(url=f"/admin/kits/{kit_id}?err={err_q}", status_code=303)
            return RedirectResponse(url="/admin/kits?err=" + err_q, status_code=303)
        before = SimpleNamespace(discount_percent=kit.discount_percent)
        kit.discount_percent = 0
        kit.updated_at = datetime.utcnow()
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
            return RedirectResponse(url=f"/admin/kits/{kit_id}?msg=saved", status_code=303)
        return RedirectResponse(url="/admin/kits?msg=saved", status_code=303)
    cost = float(ct)
    max_pct = max_kit_discount_percent_allowed(price, cost) if price > 0 else 0
    if price > 0 and discount > max_pct:
        err_q = quote(
            f"Скидка в процентах не больше {max_pct}% (по марже «цена − себестоимость» с ЗП мастеров).",
            safe="",
        )
        if red == "detail":
            return RedirectResponse(url=f"/admin/kits/{kit_id}?err={err_q}", status_code=303)
        return RedirectResponse(url="/admin/kits?err=" + err_q, status_code=303)
    before = SimpleNamespace(discount_percent=kit.discount_percent)
    kit.discount_percent = discount
    kit.updated_at = datetime.utcnow()
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
        return RedirectResponse(url=f"/admin/kits/{kit_id}?msg=saved", status_code=303)
    return RedirectResponse(url="/admin/kits?msg=saved", status_code=303)


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
        before = SimpleNamespace(
            sku=kit.sku,
            title=kit.title,
            description=kit.description,
            notes=kit.notes,
            blank_type_de=kit.blank_type_de,
            blank_type_se=kit.blank_type_se,
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
        sync_kit_authors(db, kit, form)
        # Authors changed via relationship; snapshot after sync.
        after_auth_ids = sorted([l.user_id for l in (kit.author_staff_links or [])])
        kit.updated_at = datetime.utcnow()
        kit.updated_by_user_id = current_user.id
        after = SimpleNamespace(
            sku=kit.sku,
            title=kit.title,
            description=kit.description,
            notes=kit.notes,
            blank_type_de=kit.blank_type_de,
            blank_type_se=kit.blank_type_se,
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
                "description",
                "notes",
                "blank_type_de",
                "blank_type_se",
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
                staff_for_kit_authors=list_masters_for_kit_author_pick(db),
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
        before = SimpleNamespace(
            reserved_at=kit.reserved_at,
            reserved_by_user_id=kit.reserved_by_user_id,
            reserved_for_client_id=kit.reserved_for_client_id,
            reserved_for_user_id=kit.reserved_for_user_id,
        )
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
        kit.updated_at = datetime.utcnow()
        kit.updated_by_user_id = current_user.id
        write_audit_rows(
            db,
            log_model=KitAuditLog,
            entity_field="kit_id",
            entity_id=kit.id,
            changed_by_user_id=current_user.id,
            changes=diff_fields(
                before,
                kit,
                (
                    "reserved_at",
                    "reserved_by_user_id",
                    "reserved_for_client_id",
                    "reserved_for_user_id",
                ),
            ),
        )
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

    before = SimpleNamespace(
        reserved_at=kit.reserved_at,
        reserved_by_user_id=kit.reserved_by_user_id,
        reserved_for_client_id=kit.reserved_for_client_id,
        reserved_for_user_id=kit.reserved_for_user_id,
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
        if not u or not user_has_any_role(
            db, uid, UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER
        ):
            return RedirectResponse(
                url=redirect_base + "?err=" + quote("Сотрудник не найден.", safe=""),
                status_code=303,
            )

    kit.reserved_at = datetime.utcnow()
    kit.reserved_by_user_id = current_user.id
    kit.reserved_for_client_id = cid
    kit.reserved_for_user_id = uid
    kit.updated_at = datetime.utcnow()
    kit.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=KitAuditLog,
        entity_field="kit_id",
        entity_id=kit.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            kit,
            (
                "reserved_at",
                "reserved_by_user_id",
                "reserved_for_client_id",
                "reserved_for_user_id",
            ),
        ),
    )
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
    msg: str | None = None,
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

    audit_rows = list(
        db.scalars(
            select(VisitAuditLog)
            .options(selectinload(VisitAuditLog.changed_by_user))
            .where(VisitAuditLog.visit_id == visit_id)
            .order_by(VisitAuditLog.changed_at.desc(), VisitAuditLog.id.desc())
            .limit(200)
        ).all()
    )

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

    v_policy = visit_client_change_policy(visit, current_user, db)

    return templates.TemplateResponse(
        "admin_visit_detail.html",
        _ctx(
            request,
            current_user=current_user,
            visit=visit,
            audit_rows=audit_rows,
            service_displays=service_displays,
            mix_bonus_master_label=mix_bonus_master_label,
            mix_source_ru=ru_mix_source(visit.mix_source),
            mix_complexity_ru=ru_mix_complexity(getattr(visit, "mix_complexity", None)),
            materials_used_ru="Да" if (visit.kanekalon_grams > 0 or visit.kudri_grams > 0) else "Нет",
            kit_usages_note=kit_usages_empty_explanation(),
            visit_creator_label=visit_creator_label,
            duration_h=duration_h,
            duration_m=duration_m,
            client_err=client_err,
            msg=msg,
            visit_edit_blocked=not v_policy.can_change,
            visit_edit_block_msg=v_policy.message_when_blocked,
        ),
    )


def _visit_cancel_revert_stock(db: Session, visit: Visit) -> tuple[bool, str]:
    """Revert stock kit usages for a visit. Two-pass: validate then apply."""
    usages = list(getattr(visit, "kit_usages", []) or [])
    if not usages:
        return True, ""
    kit_rows: list[tuple[Kit, int]] = []
    for u in usages:
        kit = getattr(u, "kit", None) or db.get(Kit, u.kit_id)
        if not kit:
            return False, "Не найден комплект для отката списания (kit_id)."
        pieces = int(u.pieces_used or 0)
        if pieces <= 0:
            continue
        new_avail = int(kit.pieces_available + pieces)
        if int(kit.pieces_total) >= 0 and new_avail > int(kit.pieces_total):
            return (
                False,
                f"Нельзя отменить визит: возврат превысит остаток 'всего' по комплекту {kit.sku}.",
            )
        kit_rows.append((kit, pieces))
    for kit, pieces in kit_rows:
        kit.pieces_available = int(kit.pieces_available + pieces)
        if kit.pieces_available > 0:
            kit.is_in_stock = True
    return True, ""


@app.post("/admin/visits/{visit_id}/cancel")
async def admin_visit_cancel(
    visit_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    visit = db.scalar(
        select(Visit)
        .options(selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit))
        .where(Visit.id == visit_id)
    )
    if not visit:
        raise HTTPException(status_code=404, detail="Визит не найден")
    if visit.is_cancelled:
        return RedirectResponse(url=f"/admin/visits/{visit_id}?msg=already_cancelled", status_code=303)
    if is_in_closed_payroll_period(db, visit.created_at):
        return RedirectResponse(url=f"/admin/visits/{visit_id}?msg=cancel_closed_period", status_code=303)

    ok, err = _visit_cancel_revert_stock(db, visit)
    if not ok:
        return RedirectResponse(url=f"/admin/visits/{visit_id}?msg=cancel_conflict", status_code=303)

    before = SimpleNamespace(
        is_cancelled=visit.is_cancelled,
        cancelled_at=visit.cancelled_at,
        cancelled_by_user_id=visit.cancelled_by_user_id,
    )
    visit.is_cancelled = True
    visit.cancelled_at = datetime.utcnow()
    visit.cancelled_by_user_id = current_user.id
    visit.updated_at = datetime.utcnow()
    visit.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=VisitAuditLog,
        entity_field="visit_id",
        entity_id=visit.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, visit, ("is_cancelled", "cancelled_at", "cancelled_by_user_id")),
    )
    storno_source_accruals(db, PayrollFundSourceKind.VISIT, visit.id, current_user.id)
    db.commit()
    return RedirectResponse(url=f"/admin/visits/{visit_id}?msg=cancelled", status_code=303)


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

    def _wr_float(key: str, default: float) -> float:
        r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)))
        if not r:
            return default
        try:
            v = json.loads(r.value_json)
            return float(v)
        except Exception:
            return default

    work_rates = {
        "studio_share": _wr_float("studio_share", 0.30),
        "mix_simple": _wr_float("mix_simple", 1.0),
        "mix_medium": _wr_float("mix_medium", 1.5),
        "mix_hard": _wr_float("mix_hard", 2.0),
        "custom_order_bonus_multiplier": _wr_float("custom_order_bonus_multiplier", 1.0),
    }

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
            work_rates=work_rates,
            work_rates_open=False,
            work_rates_saved=False,
            work_rates_error=None,
            payroll_open=False,
        ),
    )


def _payroll_period_day_start(d: date) -> datetime:
    return datetime.combine(d, time.min)


def _payroll_period_day_end(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59, 999999))


@app.get("/admin/reports", response_class=HTMLResponse)
def admin_operational_report_page(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    closed_periods = list_closed_payroll_periods(db)
    today = date.today()
    month_start = today.replace(day=1)
    mode = (report_mode or "custom_dates").strip()
    if mode not in ("payroll_period", "custom_dates"):
        mode = "custom_dates"

    selected_period_id: int | None = None
    d0: date
    d1: date

    pid: int | None = None
    if period_id and str(period_id).strip().isdigit():
        pid = int(str(period_id).strip())

    def _from_form_dates() -> tuple[date, date]:
        try:
            d_a = date.fromisoformat(df) if df else month_start
            d_b = date.fromisoformat(dt) if dt else today
        except ValueError:
            d_a, d_b = month_start, today
        return d_a, d_b

    if mode == "payroll_period":
        if pid is not None and pid > 0:
            p = db.get(PayrollPeriod, pid)
            if p is not None and p.closed_at is not None:
                selected_period_id = p.id
                d0 = p.date_from.date()
                d1 = p.date_to.date()
            else:
                selected_period_id = None
                d0, d1 = _from_form_dates()
        else:
            selected_period_id = None
            d0, d1 = _from_form_dates()
    else:
        selected_period_id = None
        d0, d1 = _from_form_dates()

    if d1 < d0:
        d0, d1 = d1, d0

    report = build_operational_report(db, d0, d1)
    report_dict = result_to_template_dict(report)
    return templates.TemplateResponse(
        "admin_operational_report.html",
        _ctx(
            request,
            current_user=current_user,
            title="Отчёт",
            closed_periods=closed_periods,
            report_mode=mode,
            selected_period_id=selected_period_id,
            form_df=d0.isoformat(),
            form_dt=d1.isoformat(),
            **report_dict,
        ),
    )


def _payroll_msg_ru(code: str | None) -> str | None:
    if not code:
        return None
    return {
        "created": "Открыт новый период.",
        "closed": "Период закрыт.",
        "already_closed": "Период уже был закрыт.",
    }.get(code, code)


def _payroll_err_ru(code: str | None) -> str | None:
    if not code:
        return None
    return {
        "bad_date": "Некорректная дата.",
        "range": "Дата «По» не может быть раньше даты «С».",
        "not_found": "Период не найден.",
        "open_exists": "Сначала закройте текущий открытый период.",
        "empty_po": "Укажите дату «По», чтобы закрыть период.",
    }.get(code, code)


@app.get("/admin/payroll-periods", response_class=HTMLResponse)
def admin_payroll_periods_list(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    periods = list(
        db.scalars(select(PayrollPeriod).order_by(PayrollPeriod.date_from.asc(), PayrollPeriod.id.asc())).all()
    )
    has_open = any(p.closed_at is None for p in periods)
    can_open_next = not has_open
    return templates.TemplateResponse(
        "admin_payroll_periods.html",
        _ctx(
            request,
            current_user=current_user,
            periods=periods,
            msg=_payroll_msg_ru(msg),
            err=_payroll_err_ru(err),
            can_open_next=can_open_next,
        ),
    )


@app.post("/admin/payroll-periods/open-next")
def admin_payroll_periods_open_next(
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    if db.scalar(select(PayrollPeriod.id).where(PayrollPeriod.closed_at.is_(None)).limit(1)) is not None:
        return RedirectResponse(url="/admin/payroll-periods?err=open_exists", status_code=303)

    last = db.scalar(
        select(PayrollPeriod).order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc()).limit(1)
    )
    if last is None:
        df_d = datetime.utcnow().date()
    else:
        df_d = last.date_to.date() + timedelta(days=1)

    day_start = _payroll_period_day_start(df_d)
    db.add(PayrollPeriod(date_from=day_start, date_to=day_start, closed_at=None))
    db.commit()
    return RedirectResponse(url="/admin/payroll-periods?msg=created", status_code=303)


@app.post("/admin/payroll-periods/{period_id}/close")
async def admin_payroll_periods_close(
    period_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    p = db.get(PayrollPeriod, period_id)
    if not p:
        return RedirectResponse(url="/admin/payroll-periods?err=not_found", status_code=303)
    if p.closed_at:
        return RedirectResponse(url="/admin/payroll-periods?msg=already_closed", status_code=303)

    form = await request.form()
    raw = str(form.get("date_to") or "").strip()
    if not raw:
        return RedirectResponse(url="/admin/payroll-periods?err=empty_po", status_code=303)
    try:
        d_to = date.fromisoformat(raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-periods?err=bad_date", status_code=303)

    if d_to < p.date_from.date():
        return RedirectResponse(url="/admin/payroll-periods?err=range", status_code=303)

    p.date_to = _payroll_period_day_end(d_to)
    p.closed_at = datetime.utcnow()
    p.closed_by_name = current_user.display_name
    p.closed_by_role = current_user.role.value
    db.commit()
    return RedirectResponse(url="/admin/payroll-periods?msg=closed", status_code=303)


def _payroll_fund_msg_ru(code: str | None) -> str | None:
    return {
        "paid": "Выплата записана в журнал.",
    }.get(code or "", code)


def _payroll_fund_err_ru(code: str | None) -> str | None:
    return {
        "bad_side": "Укажите корректный фонд-источник.",
        "bad_amount": "Укажите ненулевую сумму (для возврата в фонд можно ввести отрицательное число).",
        "bad_user": "Выберите сотрудника.",
        "bad_payment": "Укажите тип оплаты.",
    }.get(code or "", code)


@app.get("/admin/payroll-fund", response_class=HTMLResponse)
def admin_payroll_fund_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    masters_bal, studio_bal = ledger_balances(db)
    uids = [m["user_id"] for m in masters_bal]
    users_by_id: dict[int, User] = {}
    if uids:
        for u in db.scalars(select(User).where(User.id.in_(uids))).all():
            users_by_id[u.id] = u
    master_rows: list[dict[str, Any]] = []
    for m in masters_bal:
        uid = int(m["user_id"])
        u = users_by_id.get(uid)
        master_rows.append(
            {
                "user_id": uid,
                "display_name": (u.display_name if u else f"ID {uid}"),
                "balance": m["balance"],
            }
        )
    ledger_rows = recent_ledger_rows(db)
    payout_users = list(
        db.scalars(
            select_users_with_any_role(
                UserRole.MASTER,
                UserRole.ADMIN,
                UserRole.ADMIN_SUPER,
            ).order_by(User.display_name.asc())
        ).all()
    )
    payout_user_options: list[dict[str, Any]] = []
    for u in payout_users:
        payout_user_options.append(
            {
                "user": u,
                "roles_ru": ru_user_roles_payout_suffix(get_roles_for_user(db, u.id)),
            }
        )
    bal_by_uid = {int(m["user_id"]): float(m["balance"]) for m in master_rows}
    payout_employee_balances = {
        str(o["user"].id): round(float(bal_by_uid.get(o["user"].id, 0.0)), 2) for o in payout_user_options
    }
    payout_fund_balances_json = json.dumps(
        {"studio": round(float(studio_bal), 2), "employees": payout_employee_balances},
        ensure_ascii=False,
    )
    return templates.TemplateResponse(
        "admin_payroll_fund.html",
        _ctx(
            request,
            current_user=current_user,
            master_rows=master_rows,
            studio_balance=studio_bal,
            ledger_rows=ledger_rows,
            payout_user_options=payout_user_options,
            payout_fund_balances_json=payout_fund_balances_json,
            msg=_payroll_fund_msg_ru(msg),
            err=_payroll_fund_err_ru(err),
        ),
    )


@app.post("/admin/payroll-fund/payout")
async def admin_payroll_fund_payout(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    side_raw = (str(form.get("side") or "")).strip().upper()
    try:
        side = PayrollFundSide(side_raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_side", status_code=303)
    amount_raw = str(form.get("amount") or "").strip().replace(",", ".")
    try:
        amount = float(amount_raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_amount", status_code=303)
    comment = str(form.get("comment") or "").strip()
    raw_uid = str(form.get("user_id") or "").strip()
    if not raw_uid.isdigit():
        return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
    user_id = int(raw_uid)
    if db.get(User, user_id) is None:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
    if not user_has_any_role(db, user_id, UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
        return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
    pay_raw = (str(form.get("payment_kind") or "")).strip().upper()
    try:
        payment_kind = PayrollFundPayoutPaymentKind(pay_raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_payment", status_code=303)
    try:
        post_payout(
            db,
            side=side,
            user_id=user_id,
            amount=amount,
            created_by_user_id=current_user.id,
            comment=comment,
            payout_payment_kind=payment_kind,
        )
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_amount", status_code=303)
    db.commit()
    return RedirectResponse(url="/admin/payroll-fund?msg=paid", status_code=303)


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

    now = datetime.utcnow()

    row = db.get(Setting, "salon_cut_pct")
    before_salon = SimpleNamespace(value=(row.value if row else None))
    if not row:
        row = Setting(key="salon_cut_pct", value=str(pct))
        db.add(row)
    else:
        row.value = str(pct)
    row.updated_at = now
    row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=row.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_salon, row, ("value",)),
    )

    try:
        days = int(edit_window_days.strip())
    except ValueError:
        days = -1
    if days < 0 or days > 365:
        return RedirectResponse(url="/admin/settings?saved=0", status_code=303)
    drow = db.get(Setting, "edit_window_days")
    before_days = SimpleNamespace(value=(drow.value if drow else None))
    if not drow:
        drow = Setting(key="edit_window_days", value=str(days))
        db.add(drow)
    else:
        drow.value = str(days)
    drow.updated_at = now
    drow.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=drow.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_days, drow, ("value",)),
    )
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
    before_tz = SimpleNamespace(value=(tz_row.value if tz_row else None))
    if not tz_row:
        tz_row = Setting(key="display_timezone", value=tz_raw)
        db.add(tz_row)
    else:
        tz_row.value = tz_raw
    tz_row.updated_at = now
    tz_row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=tz_row.key,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before_tz, tz_row, ("value",)),
    )

    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)


@app.post("/admin/settings/work-rates")
async def admin_settings_work_rates_save(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()

    def _p(name: str, default: float = 0.0) -> float:
        try:
            return float(str(form.get(name) or str(default)).strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"Некорректное число: {name}")

    try:
        studio_share = _p("studio_share", 0.30)
        if studio_share < 0 or studio_share > 1:
            raise ValueError("Доля студии должна быть в диапазоне 0..1.")

        payload: dict[str, float] = {
            "studio_share": studio_share,
            "mix_simple": _p("mix_simple", 1.0),
            "mix_medium": _p("mix_medium", 1.5),
            "mix_hard": _p("mix_hard", 2.0),
            "custom_order_bonus_multiplier": _p("custom_order_bonus_multiplier", 1.0),
        }
        for k, v in payload.items():
            if v < 0:
                raise ValueError(f"Значение не может быть отрицательным: {k}")
    except ValueError as exc:
        # Re-render settings with error and open section
        # Reuse GET handler logic by collecting current values and overriding with submitted where possible.
        salon = db.get(Setting, "salon_cut_pct")
        salon_cut_pct = salon.value if salon else "0.3"
        edit_days = db.get(Setting, "edit_window_days")
        edit_window_days = edit_days.value if edit_days else "2"
        pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
        pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
        kanek_per_100 = str((pk.price_per_gram * 100) if pk else 400.0)
        kudri_per_100 = str((pku.price_per_gram * 100) if pku else 800.0)
        display_tz = get_display_timezone(db)

        def _safe(name: str, d: float) -> float:
            try:
                return float(str(form.get(name) or str(d)).strip().replace(",", "."))
            except ValueError:
                return d

        work_rates = {
            "studio_share": _safe("studio_share", 0.30),
            "mix_simple": _safe("mix_simple", 1.0),
            "mix_medium": _safe("mix_medium", 1.5),
            "mix_hard": _safe("mix_hard", 2.0),
            "custom_order_bonus_multiplier": _safe("custom_order_bonus_multiplier", 1.0),
        }
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
                saved=False,
                work_rates=work_rates,
                work_rates_open=True,
                work_rates_saved=False,
                work_rates_error=str(exc),
                payroll_open=False,
            ),
            status_code=400,
        )

    now = datetime.utcnow()
    for k, v in payload.items():
        row = db.scalar(select(WorkRate).where(WorkRate.key == k))
        if not row:
            row = WorkRate(
                key=k,
                value_json=json.dumps(v, ensure_ascii=False),
                is_active=True,
                updated_at=now,
                updated_by_user_id=current_user.id,
            )
            db.add(row)
            db.flush()
            before = SimpleNamespace(value_json=None, is_active=None)
        else:
            before = SimpleNamespace(value_json=row.value_json, is_active=row.is_active)
            row.value_json = json.dumps(v, ensure_ascii=False)
            row.is_active = True
            row.updated_at = now
            row.updated_by_user_id = current_user.id
        write_audit_rows(
            db,
            log_model=WorkRateAuditLog,
            entity_field="work_rate_id",
            entity_id=row.id,
            changed_by_user_id=current_user.id,
            changes=diff_fields(before, row, ("value_json", "is_active")),
        )
    db.commit()
    return RedirectResponse(url="/admin/settings?saved=1", status_code=303)

