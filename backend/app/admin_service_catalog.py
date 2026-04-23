"""
Суперадмин: управление прайсом (категории / подкатегории / позиции).
В одной модели и услуги для визита, и товары без визита.
Цены: три уровня (младший / мастер / старший), у каждого от и до; пустые поля → NULL в БД.
"""

from __future__ import annotations

from urllib.parse import quote
from datetime import datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.audit import diff_fields, write_audit_rows
from app.db.models import (
    Service,
    ServiceAuditLog,
    ServiceCategory,
    ServiceCategoryAuditLog,
    ServiceSubcategory,
    ServiceSubcategoryAuditLog,
    UserRole,
)
from app.db.session import get_db
from app.ru_labels import ru_master_level, ru_user_role
from app.time_utils import utcnow_naive

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_master_level"] = ru_master_level
templates.env.globals["ru_user_role"] = ru_user_role

router = APIRouter(prefix="/admin/catalog", tags=["admin-catalog"])

_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))
_PRODUCT_CATALOG_ONLY_CATEGORIES = {"Заказ", "Продажа материала"}


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _parse_optional_price(raw: object) -> float | None:
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    return float(t.replace(",", "."))


def _fmt_price_input(v: float | None) -> str:
    if v is None:
        return ""
    return str(v).replace(".", ",")


def _is_checked(raw: object | None) -> bool:
    if raw is None:
        return False
    return str(raw).lower() in ("1", "on", "true", "yes")


def _parse_kit_section_override(raw: object | None) -> bool | None:
    """Пусто → наследовать от подкатегории (NULL в БД)."""
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    low = t.lower()
    if low in ("1", "on", "true", "yes"):
        return True
    if low in ("0", "off", "false", "no"):
        return False
    return None


def _parse_tail_section_override(raw: object | None) -> bool | None:
    """Пусто → наследовать от подкатегории (NULL в БД)."""
    return _parse_kit_section_override(raw)


@router.get("", response_class=HTMLResponse)
def catalog_index(
    request: Request,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    raw = db.execute(
        select(
            ServiceCategory,
            func.count(ServiceSubcategory.id).label("sub_count"),
        )
        .outerjoin(ServiceSubcategory, ServiceSubcategory.category_id == ServiceCategory.id)
        .group_by(ServiceCategory.id)
        .order_by(ServiceCategory.id)
    ).all()
    cat_rows = [
        {"category": c, "sub_count": int(n)}
        for c, n in raw
        if (c.name or "").strip() not in _PRODUCT_CATALOG_ONLY_CATEGORIES
    ]
    return templates.TemplateResponse(
        "admin_catalog_index.html",
        _ctx(request, current_user, cat_rows=cat_rows, err=err),
    )


@router.get("/categories/new", response_class=HTMLResponse)
def category_new_form(
    request: Request,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
):
    return templates.TemplateResponse(
        "admin_catalog_category_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=True,
            category=None,
            form_name="",
            form_active=True,
            form_include_in_visit=True,
        ),
    )


@router.post("/categories/new")
def category_new_save(
    name: str = Form(...),
    is_active: str | None = Form(None),
    include_in_visit: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/admin/catalog/categories/new?err=empty", status_code=303)
    cat = ServiceCategory(
        name=nm,
        is_active=_is_checked(is_active),
        include_in_visit=_is_checked(include_in_visit),
    )
    db.add(cat)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(url="/admin/catalog/categories/new?err=duplicate", status_code=303)
    return RedirectResponse(url=f"/admin/catalog/categories/{cat.id}/subcategories", status_code=303)


@router.get("/categories/{category_id}/edit", response_class=HTMLResponse)
def category_edit_form(
    request: Request,
    category_id: int,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    return templates.TemplateResponse(
        "admin_catalog_category_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=False,
            category=cat,
            form_name=cat.name,
            form_active=cat.is_active,
            form_include_in_visit=cat.include_in_visit,
        ),
    )


@router.post("/categories/{category_id}/edit")
def category_edit_save(
    category_id: int,
    name: str = Form(...),
    is_active: str | None = Form(None),
    include_in_visit: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    before = SimpleNamespace(name=cat.name, is_active=cat.is_active, include_in_visit=cat.include_in_visit)
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=empty",
            status_code=303,
        )
    cat.name = nm
    cat.is_active = _is_checked(is_active)
    cat.include_in_visit = _is_checked(include_in_visit)
    cat.updated_at = utcnow_naive()
    cat.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceCategoryAuditLog,
        entity_field="category_id",
        entity_id=cat.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, cat, ("name", "is_active", "include_in_visit")),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/categories/{category_id}/subcategories", status_code=303)


@router.get("/categories/{category_id}/subcategories", response_class=HTMLResponse)
def subcategory_list(
    request: Request,
    category_id: int,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    subs = (
        db.scalars(
            select(ServiceSubcategory)
            .where(ServiceSubcategory.category_id == category_id)
            .order_by(ServiceSubcategory.id)
        )
        .all()
    )
    return templates.TemplateResponse(
        "admin_catalog_subcategories.html",
        _ctx(request, current_user, category=cat, subs=subs, err=err),
    )


@router.get("/subcategories/new", response_class=HTMLResponse)
def subcategory_new_form(
    request: Request,
    category_id: int = Query(...),
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    return templates.TemplateResponse(
        "admin_catalog_subcategory_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=True,
            category=cat,
            sub=None,
            form_name="",
            form_active=True,
            form_show_kit=False,
            form_show_tail=False,
            form_show_material=True,
            form_show_thermo=False,
        ),
    )


@router.post("/subcategories/new")
def subcategory_new_save(
    category_id: int = Form(...),
    name: str = Form(...),
    is_active: str | None = Form(None),
    show_kit_section: str | None = Form(None),
    show_tail_section: str | None = Form(None),
    show_material_description: str | None = Form(None),
    show_thermo_visit: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    if (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    nm = (name or "").strip()
    if not nm:
        q = quote(str(category_id))
        return RedirectResponse(url=f"/admin/catalog/subcategories/new?category_id={q}&err=empty", status_code=303)
    sub = ServiceSubcategory(
        category_id=category_id,
        name=nm,
        is_active=_is_checked(is_active),
        show_kit_section=_is_checked(show_kit_section),
        show_tail_section=_is_checked(show_tail_section),
        show_material_description=_is_checked(show_material_description),
        show_thermo_visit=_is_checked(show_thermo_visit),
    )
    db.add(sub)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        q = quote(str(category_id))
        return RedirectResponse(url=f"/admin/catalog/subcategories/new?category_id={q}&err=duplicate", status_code=303)
    return RedirectResponse(url=f"/admin/catalog/subcategories/{sub.id}/services", status_code=303)


@router.get("/subcategories/{subcategory_id}/edit", response_class=HTMLResponse)
def subcategory_edit_form(
    request: Request,
    subcategory_id: int,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    return templates.TemplateResponse(
        "admin_catalog_subcategory_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=False,
            category=cat,
            sub=sub,
            form_name=sub.name,
            form_active=sub.is_active,
            form_show_kit=sub.show_kit_section,
            form_show_tail=getattr(sub, "show_tail_section", False),
            form_show_material=sub.show_material_description,
            form_show_thermo=sub.show_thermo_visit,
        ),
    )


@router.post("/subcategories/{subcategory_id}/edit")
def subcategory_edit_save(
    subcategory_id: int,
    name: str = Form(...),
    is_active: str | None = Form(None),
    show_kit_section: str | None = Form(None),
    show_tail_section: str | None = Form(None),
    show_material_description: str | None = Form(None),
    show_thermo_visit: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    before = SimpleNamespace(
        name=sub.name,
        is_active=sub.is_active,
        show_kit_section=sub.show_kit_section,
        show_tail_section=getattr(sub, "show_tail_section", False),
        show_material_description=sub.show_material_description,
        show_thermo_visit=sub.show_thermo_visit,
    )
    nm = (name or "").strip()
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/edit?err=empty",
            status_code=303,
        )
    sub.name = nm
    sub.is_active = _is_checked(is_active)
    sub.show_kit_section = _is_checked(show_kit_section)
    sub.show_tail_section = _is_checked(show_tail_section)
    sub.show_material_description = _is_checked(show_material_description)
    sub.show_thermo_visit = _is_checked(show_thermo_visit)
    sub.updated_at = utcnow_naive()
    sub.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceSubcategoryAuditLog,
        entity_field="subcategory_id",
        entity_id=sub.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            sub,
            (
                "name",
                "is_active",
                "show_kit_section",
                "show_tail_section",
                "show_material_description",
                "show_thermo_visit",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/admin/catalog/categories/{sub.category_id}/subcategories",
        status_code=303,
    )


@router.get("/subcategories/{subcategory_id}/services", response_class=HTMLResponse)
def service_list(
    request: Request,
    subcategory_id: int,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    services = (
        db.scalars(
            select(Service).where(Service.subcategory_id == subcategory_id).order_by(Service.id)
        ).all()
    )
    return templates.TemplateResponse(
        "admin_catalog_services.html",
        _ctx(request, current_user, category=cat, subcategory=sub, services=services, err=err),
    )


@router.get("/services/new", response_class=HTMLResponse)
def service_new_form(
    request: Request,
    subcategory_id: int = Query(...),
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    return templates.TemplateResponse(
        "admin_catalog_service_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=True,
            category=cat,
            subcategory=sub,
            svc=None,
            form_name="",
            form_active=True,
            form_jf="",
            form_jt="",
            form_mf="",
            form_mt="",
            form_sf="",
            form_st="",
            form_master_pay="",
            form_studio_pay="",
            form_fixed_exp="",
            form_is_per_unit=False,
            form_unit_label="",
            form_kit_override="",
            form_tail_override="",
            form_hide_material=False,
            form_order_rubber=False,
            form_retail_kanekalon=False,
            form_retail_kudri=False,
            form_retail_mix=False,
        ),
    )


@router.post("/services/new")
def service_new_save(
    subcategory_id: int = Form(...),
    name: str = Form(...),
    is_active: str | None = Form(None),
    price_junior_from: str | None = Form(None),
    price_junior_to: str | None = Form(None),
    price_middle_from: str | None = Form(None),
    price_middle_to: str | None = Form(None),
    price_senior_from: str | None = Form(None),
    price_senior_to: str | None = Form(None),
    master_pay_amount: str | None = Form(None),
    studio_pay_amount: str | None = Form(None),
    fixed_expense_amount: str | None = Form(None),
    is_per_unit: str | None = Form(None),
    unit_label: str | None = Form(None),
    kit_section_override: str | None = Form(None),
    tail_section_override: str | None = Form(None),
    hide_material_description: str | None = Form(None),
    order_rubber_extra_time_amort: str | None = Form(None),
    retail_material_kanekalon: str | None = Form(None),
    retail_material_kudri: str | None = Form(None),
    retail_material_mix: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    cat = db.get(ServiceCategory, sub.category_id)
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    nm = (name or "").strip()
    if not nm:
        q = quote(str(subcategory_id))
        return RedirectResponse(url=f"/admin/catalog/services/new?subcategory_id={q}&err=empty", status_code=303)
    try:
        jf = _parse_optional_price(price_junior_from)
        jt = _parse_optional_price(price_junior_to)
        mf = _parse_optional_price(price_middle_from)
        mt = _parse_optional_price(price_middle_to)
        sf = _parse_optional_price(price_senior_from)
        st = _parse_optional_price(price_senior_to)
    except ValueError:
        q = quote(str(subcategory_id))
        return RedirectResponse(url=f"/admin/catalog/services/new?subcategory_id={q}&err=bad_price", status_code=303)

    svc = Service(
        subcategory_id=subcategory_id,
        name=nm,
        is_active=_is_checked(is_active),
        price_junior_from=jf,
        price_junior_to=jt,
        price_middle_from=mf,
        price_middle_to=mt,
        price_senior_from=sf,
        price_senior_to=st,
        master_pay_amount=_parse_optional_price(master_pay_amount),
        studio_pay_amount=_parse_optional_price(studio_pay_amount),
        fixed_expense_amount=_parse_optional_price(fixed_expense_amount),
        is_per_unit=_is_checked(is_per_unit),
        unit_label=(unit_label or "").strip()[:60] or None,
        kit_section_override=_parse_kit_section_override(kit_section_override),
        tail_section_override=_parse_tail_section_override(tail_section_override),
        hide_material_description=_is_checked(hide_material_description),
        order_rubber_extra_time_amort=_is_checked(order_rubber_extra_time_amort),
        retail_material_kanekalon=_is_checked(retail_material_kanekalon),
        retail_material_kudri=_is_checked(retail_material_kudri),
        retail_material_mix=_is_checked(retail_material_mix),
    )
    db.add(svc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        q = quote(str(subcategory_id))
        return RedirectResponse(
            url=f"/admin/catalog/services/new?subcategory_id={q}&err=duplicate",
            status_code=303,
        )
    # Поля анкеты услуги — отдельный шаг; при создании строк нет (общая форма подкатегории подтягивается позже).
    return RedirectResponse(url=f"/admin/catalog/subcategories/{subcategory_id}/services", status_code=303)


@router.get("/services/{service_id}/edit", response_class=HTMLResponse)
def service_edit_form(
    request: Request,
    service_id: int,
    err: str | None = None,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    return templates.TemplateResponse(
        "admin_catalog_service_form.html",
        _ctx(
            request,
            current_user,
            err=err,
            is_new=False,
            category=cat,
            subcategory=sub,
            svc=svc,
            form_name=svc.name,
            form_active=svc.is_active,
            form_jf=_fmt_price_input(svc.price_junior_from),
            form_jt=_fmt_price_input(svc.price_junior_to),
            form_mf=_fmt_price_input(svc.price_middle_from),
            form_mt=_fmt_price_input(svc.price_middle_to),
            form_sf=_fmt_price_input(svc.price_senior_from),
            form_st=_fmt_price_input(svc.price_senior_to),
            form_master_pay=_fmt_price_input(svc.master_pay_amount),
            form_studio_pay=_fmt_price_input(svc.studio_pay_amount),
            form_fixed_exp=_fmt_price_input(svc.fixed_expense_amount),
            form_is_per_unit=bool(svc.is_per_unit),
            form_unit_label=(svc.unit_label or ""),
            form_kit_override=(
                ""
                if svc.kit_section_override is None
                else ("1" if svc.kit_section_override else "0")
            ),
            form_tail_override=(
                ""
                if getattr(svc, "tail_section_override", None) is None
                else ("1" if svc.tail_section_override else "0")
            ),
            form_hide_material=svc.hide_material_description,
            form_order_rubber=svc.order_rubber_extra_time_amort,
            form_retail_kanekalon=svc.retail_material_kanekalon,
            form_retail_kudri=svc.retail_material_kudri,
            form_retail_mix=svc.retail_material_mix,
        ),
    )


@router.post("/services/{service_id}/edit")
def service_edit_save(
    service_id: int,
    name: str = Form(...),
    is_active: str | None = Form(None),
    price_junior_from: str | None = Form(None),
    price_junior_to: str | None = Form(None),
    price_middle_from: str | None = Form(None),
    price_middle_to: str | None = Form(None),
    price_senior_from: str | None = Form(None),
    price_senior_to: str | None = Form(None),
    master_pay_amount: str | None = Form(None),
    studio_pay_amount: str | None = Form(None),
    fixed_expense_amount: str | None = Form(None),
    is_per_unit: str | None = Form(None),
    unit_label: str | None = Form(None),
    kit_section_override: str | None = Form(None),
    tail_section_override: str | None = Form(None),
    hide_material_description: str | None = Form(None),
    order_rubber_extra_time_amort: str | None = Form(None),
    retail_material_kanekalon: str | None = Form(None),
    retail_material_kudri: str | None = Form(None),
    retail_material_mix: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    cat = db.get(ServiceCategory, sub.category_id) if sub else None
    if cat and (cat.name or "").strip() in _PRODUCT_CATALOG_ONLY_CATEGORIES:
        return RedirectResponse(url="/products-catalog?category=" + quote(cat.name, safe=""), status_code=303)
    before = SimpleNamespace(
        name=svc.name,
        is_active=svc.is_active,
        price_junior_from=svc.price_junior_from,
        price_junior_to=svc.price_junior_to,
        price_middle_from=svc.price_middle_from,
        price_middle_to=svc.price_middle_to,
        price_senior_from=svc.price_senior_from,
        price_senior_to=svc.price_senior_to,
        master_pay_amount=svc.master_pay_amount,
        studio_pay_amount=svc.studio_pay_amount,
        fixed_expense_amount=svc.fixed_expense_amount,
        is_per_unit=svc.is_per_unit,
        unit_label=svc.unit_label,
        kit_section_override=svc.kit_section_override,
        tail_section_override=getattr(svc, "tail_section_override", None),
        hide_material_description=svc.hide_material_description,
        order_rubber_extra_time_amort=svc.order_rubber_extra_time_amort,
        retail_material_kanekalon=svc.retail_material_kanekalon,
        retail_material_kudri=svc.retail_material_kudri,
        retail_material_mix=svc.retail_material_mix,
    )
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=empty",
            status_code=303,
        )
    try:
        jf = _parse_optional_price(price_junior_from)
        jt = _parse_optional_price(price_junior_to)
        mf = _parse_optional_price(price_middle_from)
        mt = _parse_optional_price(price_middle_to)
        sf = _parse_optional_price(price_senior_from)
        st = _parse_optional_price(price_senior_to)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=bad_price",
            status_code=303,
        )

    svc.name = nm
    svc.is_active = _is_checked(is_active)
    svc.price_junior_from = jf
    svc.price_junior_to = jt
    svc.price_middle_from = mf
    svc.price_middle_to = mt
    svc.price_senior_from = sf
    svc.price_senior_to = st
    svc.master_pay_amount = _parse_optional_price(master_pay_amount)
    svc.studio_pay_amount = _parse_optional_price(studio_pay_amount)
    svc.fixed_expense_amount = _parse_optional_price(fixed_expense_amount)
    svc.is_per_unit = _is_checked(is_per_unit)
    svc.unit_label = (unit_label or "").strip()[:60] or None
    svc.kit_section_override = _parse_kit_section_override(kit_section_override)
    svc.tail_section_override = _parse_tail_section_override(tail_section_override)
    svc.hide_material_description = _is_checked(hide_material_description)
    svc.order_rubber_extra_time_amort = _is_checked(order_rubber_extra_time_amort)
    svc.retail_material_kanekalon = _is_checked(retail_material_kanekalon)
    svc.retail_material_kudri = _is_checked(retail_material_kudri)
    svc.retail_material_mix = _is_checked(retail_material_mix)
    svc.updated_at = utcnow_naive()
    svc.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceAuditLog,
        entity_field="service_id",
        entity_id=svc.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(
            before,
            svc,
            (
                "name",
                "is_active",
                "price_junior_from",
                "price_junior_to",
                "price_middle_from",
                "price_middle_to",
                "price_senior_from",
                "price_senior_to",
                "master_pay_amount",
                "studio_pay_amount",
                "fixed_expense_amount",
                "is_per_unit",
                "unit_label",
                "kit_section_override",
                "tail_section_override",
                "hide_material_description",
                "order_rubber_extra_time_amort",
                "retail_material_kanekalon",
                "retail_material_kudri",
                "retail_material_mix",
            ),
        ),
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/subcategories/{svc.subcategory_id}/services", status_code=303)
