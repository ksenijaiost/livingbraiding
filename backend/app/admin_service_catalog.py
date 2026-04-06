"""
Суперадмин: CRUD категорий / подкатегорий / услуг (каталог).
Прайс: три уровня (младший / мастер / старший), у каждого от и до; пустые поля → NULL в БД.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import ServiceCategory, ServiceSubcategory, Service, UserRole
from app.db.session import get_db
from app.ru_labels import ru_master_level

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["ru_master_level"] = ru_master_level

router = APIRouter(prefix="/admin/catalog", tags=["admin-catalog"])

_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


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
    cat_rows = [{"category": c, "sub_count": int(n)} for c, n in raw]
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
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=empty",
            status_code=303,
        )
    cat.name = nm
    cat.is_active = _is_checked(is_active)
    cat.include_in_visit = _is_checked(include_in_visit)
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
    return templates.TemplateResponse(
        "admin_catalog_subcategory_form.html",
        _ctx(request, current_user, err=err, is_new=True, category=cat, sub=None, form_name="", form_active=True),
    )


@router.post("/subcategories/new")
def subcategory_new_save(
    category_id: int = Form(...),
    name: str = Form(...),
    is_active: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    nm = (name or "").strip()
    if not nm:
        q = quote(str(category_id))
        return RedirectResponse(url=f"/admin/catalog/subcategories/new?category_id={q}&err=empty", status_code=303)
    sub = ServiceSubcategory(category_id=category_id, name=nm, is_active=_is_checked(is_active))
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
        ),
    )


@router.post("/subcategories/{subcategory_id}/edit")
def subcategory_edit_save(
    subcategory_id: int,
    name: str = Form(...),
    is_active: str | None = Form(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/subcategories/{subcategory_id}/edit?err=empty",
            status_code=303,
        )
    sub.name = nm
    sub.is_active = _is_checked(is_active)
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
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    sub = db.get(ServiceSubcategory, subcategory_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Подкатегория не найдена")
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
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    svc = db.get(Service, service_id)
    if svc is None:
        raise HTTPException(status_code=404, detail="Услуга не найдена")
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
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=duplicate",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/catalog/subcategories/{svc.subcategory_id}/services", status_code=303)
