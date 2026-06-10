"""
Суперадмин: управление прайсом (категории / подкатегории / позиции).
В одной модели и услуги для визита, и товары без визита.
Цены: три уровня (младший / мастер / старший), у каждого от и до; пустые поля → NULL в БД.
"""

from __future__ import annotations

from urllib.parse import quote
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.audit import diff_fields, write_audit_rows
from app.consultation_types import CONSULTATION_TYPE_CHOICES
from app.db.models import (
    ConsultationKind,
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
_PRICE_LEVEL_FIELDS: dict[str, tuple[str, str]] = {
    "JUNIOR": ("price_junior_from", "price_junior_to"),
    "MIDDLE": ("price_middle_from", "price_middle_to"),
    "SENIOR": ("price_senior_from", "price_senior_to"),
}


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _parse_estimated_duration(raw: object) -> int:
    if raw is None:
        raise ValueError("missing duration")
    s = str(raw).strip()
    if not s:
        raise ValueError("empty duration")
    try:
        v = int(s)
    except ValueError:
        raise ValueError("bad duration")
    if v < 1 or v > 1440:
        raise ValueError("bad duration range")
    return v


def _parse_optional_price(raw: object) -> float | None:
    if raw is None:
        return None
    t = str(raw).strip()
    if not t:
        return None
    return float(t.replace(",", "."))


def _parse_autocalc_percent(raw: object | None, *, max_pct: float = 10_000.0) -> float:
    if raw is None:
        raise ValueError("pct missing")
    t = str(raw).strip()
    if not t:
        raise ValueError("pct empty")
    v = float(t.replace(",", "."))
    if v < 0 or v > float(max_pct):
        raise ValueError("pct range")
    return v


def _round_rubles(v: float) -> float:
    if v >= 0:
        return float(int(v + 0.5))
    return float(int(v - 0.5))


def _autocalc_price(
    source: float | None,
    *,
    pct: float,
) -> float | None:
    if source is None:
        return None
    return _round_rubles(float(source) * float(pct) / 100.0)


def _allowed_service_scope_rows(db: Session) -> list[tuple[ServiceCategory, ServiceSubcategory, Service]]:
    return list(
        db.execute(
            select(ServiceCategory, ServiceSubcategory, Service)
            .join(ServiceSubcategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .join(Service, Service.subcategory_id == ServiceSubcategory.id)
            .where(ServiceCategory.name.not_in(_PRODUCT_CATALOG_ONLY_CATEGORIES))
            .order_by(ServiceCategory.name.asc(), ServiceSubcategory.name.asc(), Service.name.asc(), Service.id.asc())
        ).all()
    )


def _autocalc_scope_options(db: Session) -> dict[str, list[dict[str, Any]]]:
    rows = _allowed_service_scope_rows(db)
    categories: dict[int, dict[str, Any]] = {}
    subcategories: dict[int, dict[str, Any]] = {}
    services: list[dict[str, Any]] = []
    for cat, sub, svc in rows:
        categories[int(cat.id)] = {"id": int(cat.id), "name": cat.name}
        subcategories[int(sub.id)] = {"id": int(sub.id), "name": sub.name, "category_id": int(cat.id)}
        services.append(
            {
                "id": int(svc.id),
                "name": svc.name,
                "is_active": bool(svc.is_active),
                "category_id": int(cat.id),
                "category_name": cat.name,
                "subcategory_id": int(sub.id),
                "subcategory_name": sub.name,
            }
        )
    return {
        "categories": sorted(categories.values(), key=lambda x: str(x["name"]).lower()),
        "subcategories": sorted(subcategories.values(), key=lambda x: (x["category_id"], str(x["name"]).lower())),
        "services": services,
    }


def _resolve_autocalc_service_ids(
    db: Session,
    *,
    scope_mode: str,
    category_id: int | None,
    subcategory_id: int | None,
    service_ids: list[int],
) -> list[int]:
    rows = _allowed_service_scope_rows(db)
    allowed_cat_ids = {int(cat.id) for cat, _sub, _svc in rows}
    allowed_sub_ids = {int(sub.id) for _cat, sub, _svc in rows}
    allowed_service_ids = {int(svc.id) for _cat, _sub, svc in rows}

    if scope_mode == "all":
        return sorted(allowed_service_ids)
    if scope_mode == "category":
        if category_id is None or int(category_id) not in allowed_cat_ids:
            raise ValueError("Выберите категорию.")
        return sorted({int(svc.id) for cat, _sub, svc in rows if int(cat.id) == int(category_id)})
    if scope_mode == "subcategory":
        if category_id is None or int(category_id) not in allowed_cat_ids:
            raise ValueError("Выберите категорию.")
        if subcategory_id is None or int(subcategory_id) not in allowed_sub_ids:
            raise ValueError("Выберите подкатегорию.")
        return sorted(
            {
                int(svc.id)
                for cat, sub, svc in rows
                if int(cat.id) == int(category_id) and int(sub.id) == int(subcategory_id)
            }
        )
    if scope_mode == "services":
        clean_ids = sorted({int(i) for i in service_ids if int(i) > 0})
        if not clean_ids:
            raise ValueError("Выберите хотя бы одну услугу.")
        bad = [i for i in clean_ids if i not in allowed_service_ids]
        if bad:
            raise ValueError("Выбраны недопустимые услуги.")
        return clean_ids
    raise ValueError("Некорректный режим выбора позиций.")


def _autocalc_apply_for_services(
    db: Session,
    *,
    services: list[Service],
    source_level: str,
    target_level: str,
    pct: float,
    changed_by_user_id: int,
) -> int:
    source_from, source_to = _PRICE_LEVEL_FIELDS[source_level]
    target_from, target_to = _PRICE_LEVEL_FIELDS[target_level]
    updated_count = 0
    for svc in services:
        before = SimpleNamespace(
            price_junior_from=svc.price_junior_from,
            price_junior_to=svc.price_junior_to,
            price_middle_from=svc.price_middle_from,
            price_middle_to=svc.price_middle_to,
            price_senior_from=svc.price_senior_from,
            price_senior_to=svc.price_senior_to,
        )
        changed = False
        src_from_val = getattr(svc, source_from)
        src_to_val = getattr(svc, source_to)
        new_from = _autocalc_price(src_from_val, pct=pct)
        new_to = _autocalc_price(src_to_val, pct=pct)
        if new_from is not None and getattr(svc, target_from) != new_from:
            setattr(svc, target_from, new_from)
            changed = True
        if new_to is not None and getattr(svc, target_to) != new_to:
            setattr(svc, target_to, new_to)
            changed = True
        if not changed:
            continue
        svc.updated_at = utcnow_naive()
        svc.updated_by_user_id = changed_by_user_id
        write_audit_rows(
            db,
            log_model=ServiceAuditLog,
            entity_field="service_id",
            entity_id=svc.id,
            changed_by_user_id=changed_by_user_id,
            changes=diff_fields(
                before,
                svc,
                (
                    "price_junior_from",
                    "price_junior_to",
                    "price_middle_from",
                    "price_middle_to",
                    "price_senior_from",
                    "price_senior_to",
                ),
            ),
        )
        updated_count += 1
    return updated_count


def _render_price_autocalc_page(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    error: str | None = None,
    msg: str | None = None,
    selected_count: int | None = None,
    updated_count: int | None = None,
    fp: dict[str, Any] | None = None,
    status_code: int = 200,
):
    options = _autocalc_scope_options(db)
    data = fp or {}
    return templates.TemplateResponse(
        "admin_catalog_price_autocalc.html",
        _ctx(
            request,
            current_user,
            error=error,
            msg=msg,
            selected_count=selected_count,
            updated_count=updated_count,
            scope_options=options,
            fp={
                "scope_mode": data.get("scope_mode") or "all",
                "category_id": str(data.get("category_id") or ""),
                "subcategory_id": str(data.get("subcategory_id") or ""),
                "service_ids": [int(x) for x in (data.get("service_ids") or []) if int(x) > 0],
                "target_level": str(data.get("target_level") or "JUNIOR"),
                "source_level": str(data.get("source_level") or "MIDDLE"),
                "percent": str(data.get("percent") or "60"),
            },
        ),
        status_code=status_code,
    )


def _fmt_price_input(v: float | None) -> str:
    if v is None:
        return ""
    return str(v).replace(".", ",")


def _is_checked(raw: object | None) -> bool:
    if raw is None:
        return False
    return str(raw).lower() in ("1", "on", "true", "yes")


def _parse_tri_state(raw: object | None) -> bool | None:
    """
    Радио с 3 значениями:
    - "" / None / "inherit" → None (как у подкатегории)
    - "show" → True
    - "hide" → False
    """
    if raw is None:
        return None
    t = str(raw).strip().lower()
    if t in ("", "inherit", "none", "null"):
        return None
    if t in ("show", "1", "on", "true", "yes"):
        return True
    if t in ("hide", "0", "off", "false", "no"):
        return False
    return None


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
    svc_by_cat: dict[int, int] = {
        int(cat_id): int(cnt)
        for cat_id, cnt in db.execute(
            select(ServiceSubcategory.category_id, func.count(Service.id))
            .join(Service, Service.subcategory_id == ServiceSubcategory.id)
            .group_by(ServiceSubcategory.category_id)
        ).all()
    }
    cat_rows = [
        {
            "category": c,
            "sub_count": int(n),
            "service_count": svc_by_cat.get(c.id, 0),
        }
        for c, n in raw
        if (c.name or "").strip() not in _PRODUCT_CATALOG_ONLY_CATEGORIES
    ]
    total_services = sum(r["service_count"] for r in cat_rows)
    return templates.TemplateResponse(
        "admin_catalog_index.html",
        _ctx(
            request,
            current_user,
            cat_rows=cat_rows,
            total_services=total_services,
            err=err,
        ),
    )


@router.get("/price-autocalc", response_class=HTMLResponse)
def catalog_price_autocalc_form(
    request: Request,
    msg: str | None = None,
    selected: int | None = Query(None),
    updated: int | None = Query(None),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    return _render_price_autocalc_page(
        request,
        current_user=current_user,
        db=db,
        msg=msg,
        selected_count=selected,
        updated_count=updated,
    )


@router.post("/price-autocalc", response_class=HTMLResponse)
async def catalog_price_autocalc_apply(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    scope_mode = str(form.get("scope_mode") or "all").strip().lower()
    category_raw = str(form.get("category_id") or "").strip()
    subcategory_raw = str(form.get("subcategory_id") or "").strip()
    service_ids_raw = form.getlist("service_ids")
    target_level = str(form.get("target_level") or "").strip().upper()
    source_level = str(form.get("source_level") or "").strip().upper()
    percent_raw = str(form.get("percent") or "").strip()
    fp = {
        "scope_mode": scope_mode,
        "category_id": category_raw,
        "subcategory_id": subcategory_raw,
        "service_ids": [int(x) for x in service_ids_raw if str(x).strip().isdigit()],
        "target_level": target_level,
        "source_level": source_level,
        "percent": percent_raw,
    }

    if target_level not in _PRICE_LEVEL_FIELDS:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Выберите уровень, кому считаем.",
            fp=fp,
            status_code=400,
        )
    if source_level not in _PRICE_LEVEL_FIELDS:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Выберите уровень, от кого считаем.",
            fp=fp,
            status_code=400,
        )
    try:
        pct = _parse_autocalc_percent(percent_raw)
    except ValueError:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Процент: неотрицательное число (можно больше 100 для повышения цены).",
            fp=fp,
            status_code=400,
        )

    category_id = int(category_raw) if category_raw.isdigit() else None
    subcategory_id = int(subcategory_raw) if subcategory_raw.isdigit() else None
    service_ids = [int(x) for x in service_ids_raw if str(x).strip().isdigit()]
    try:
        resolved_service_ids = _resolve_autocalc_service_ids(
            db,
            scope_mode=scope_mode,
            category_id=category_id,
            subcategory_id=subcategory_id,
            service_ids=service_ids,
        )
    except ValueError as exc:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error=str(exc),
            fp=fp,
            status_code=400,
        )
    if not resolved_service_ids:
        return _render_price_autocalc_page(
            request,
            current_user=current_user,
            db=db,
            error="Нет услуг для выбранной области.",
            fp=fp,
            status_code=400,
        )
    services = list(
        db.scalars(select(Service).where(Service.id.in_(resolved_service_ids)).order_by(Service.id.asc())).all()
    )
    updated_count = _autocalc_apply_for_services(
        db,
        services=services,
        source_level=source_level,
        target_level=target_level,
        pct=pct,
        changed_by_user_id=current_user.id,
    )
    db.commit()
    return RedirectResponse(
        url=f"/admin/catalog/price-autocalc?msg=done&selected={len(resolved_service_ids)}&updated={updated_count}",
        status_code=303,
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
            form_consultation_kind=ConsultationKind.BRAIDING.value,
            consultation_kind_choices=CONSULTATION_TYPE_CHOICES,
        ),
    )


def _parse_consultation_kind(raw: str | None) -> ConsultationKind:
    v = (raw or "").strip().upper()
    try:
        return ConsultationKind(v)
    except ValueError:
        return ConsultationKind.BRAIDING


@router.post("/categories/new")
def category_new_save(
    name: str = Form(...),
    is_active: str | None = Form(None),
    consultation_kind: str = Form(ConsultationKind.BRAIDING.value),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(url="/admin/catalog/categories/new?err=empty", status_code=303)
    cat = ServiceCategory(
        name=nm,
        is_active=_is_checked(is_active),
        consultation_kind=_parse_consultation_kind(consultation_kind),
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
            form_include_in_visit=True,
            form_consultation_kind=cat.consultation_kind.value,
            consultation_kind_choices=CONSULTATION_TYPE_CHOICES,
        ),
    )


@router.post("/categories/{category_id}/edit")
def category_edit_save(
    category_id: int,
    name: str = Form(...),
    is_active: str | None = Form(None),
    consultation_kind: str = Form(ConsultationKind.BRAIDING.value),
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    cat = db.get(ServiceCategory, category_id)
    if cat is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    before = SimpleNamespace(
        name=cat.name,
        is_active=cat.is_active,
        consultation_kind=cat.consultation_kind,
    )
    nm = (name or "").strip()
    if not nm:
        return RedirectResponse(
            url=f"/admin/catalog/categories/{category_id}/edit?err=empty",
            status_code=303,
        )
    cat.name = nm
    cat.is_active = _is_checked(is_active)
    cat.consultation_kind = _parse_consultation_kind(consultation_kind)
    cat.updated_at = utcnow_naive()
    cat.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=ServiceCategoryAuditLog,
        entity_field="category_id",
        entity_id=cat.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, cat, ("name", "is_active", "consultation_kind")),
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
    svc_by_sub: dict[int, int] = {
        int(sub_id): int(cnt)
        for sub_id, cnt in db.execute(
            select(Service.subcategory_id, func.count(Service.id))
            .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
            .where(ServiceSubcategory.category_id == category_id)
            .group_by(Service.subcategory_id)
        ).all()
    }
    sub_rows = [
        {"sub": s, "service_count": svc_by_sub.get(s.id, 0)} for s in subs
    ]
    total_services = sum(r["service_count"] for r in sub_rows)
    return templates.TemplateResponse(
        "admin_catalog_subcategories.html",
        _ctx(
            request,
            current_user,
            category=cat,
            sub_rows=sub_rows,
            total_services=total_services,
            err=err,
        ),
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
            form_kit_override="",
            form_tail_override="",
            form_material_desc_override="",
            form_retail_kanekalon=False,
            form_retail_kudri=False,
            form_retail_mix=False,
            form_duration="120",
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
    kit_section_override: str | None = Form(None),
    tail_section_override: str | None = Form(None),
    material_description_override: str | None = Form(None),
    retail_material_kanekalon: str | None = Form(None),
    retail_material_kudri: str | None = Form(None),
    retail_material_mix: str | None = Form(None),
    estimated_duration_minutes: str = Form(...),
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
        duration = _parse_estimated_duration(estimated_duration_minutes)
    except ValueError:
        q = quote(str(subcategory_id))
        return RedirectResponse(url=f"/admin/catalog/services/new?subcategory_id={q}&err=bad_duration", status_code=303)
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
        estimated_duration_minutes=duration,
        price_junior_from=jf,
        price_junior_to=jt,
        price_middle_from=mf,
        price_middle_to=mt,
        price_senior_from=sf,
        price_senior_to=st,
        kit_section_override=_parse_kit_section_override(kit_section_override),
        tail_section_override=_parse_tail_section_override(tail_section_override),
        material_description_override=_parse_tri_state(material_description_override),
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
            form_material_desc_override=(
                ""
                if getattr(svc, "material_description_override", None) is None
                else ("show" if svc.material_description_override else "hide")
            ),
            form_retail_kanekalon=svc.retail_material_kanekalon,
            form_retail_kudri=svc.retail_material_kudri,
            form_retail_mix=svc.retail_material_mix,
            form_duration=str(svc.estimated_duration_minutes),
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
    kit_section_override: str | None = Form(None),
    tail_section_override: str | None = Form(None),
    material_description_override: str | None = Form(None),
    retail_material_kanekalon: str | None = Form(None),
    retail_material_kudri: str | None = Form(None),
    retail_material_mix: str | None = Form(None),
    estimated_duration_minutes: str = Form(...),
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
        estimated_duration_minutes=svc.estimated_duration_minutes,
        price_junior_from=svc.price_junior_from,
        price_junior_to=svc.price_junior_to,
        price_middle_from=svc.price_middle_from,
        price_middle_to=svc.price_middle_to,
        price_senior_from=svc.price_senior_from,
        price_senior_to=svc.price_senior_to,
        kit_section_override=svc.kit_section_override,
        tail_section_override=getattr(svc, "tail_section_override", None),
        material_description_override=getattr(svc, "material_description_override", None),
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
        duration = _parse_estimated_duration(estimated_duration_minutes)
    except ValueError:
        return RedirectResponse(
            url=f"/admin/catalog/services/{service_id}/edit?err=bad_duration",
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
    svc.estimated_duration_minutes = duration
    svc.price_junior_from = jf
    svc.price_junior_to = jt
    svc.price_middle_from = mf
    svc.price_middle_to = mt
    svc.price_senior_from = sf
    svc.price_senior_to = st
    svc.kit_section_override = _parse_kit_section_override(kit_section_override)
    svc.tail_section_override = _parse_tail_section_override(tail_section_override)
    svc.material_description_override = _parse_tri_state(material_description_override)
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
                "estimated_duration_minutes",
                "price_junior_from",
                "price_junior_to",
                "price_middle_from",
                "price_middle_to",
                "price_senior_from",
                "price_senior_to",
                "kit_section_override",
                "tail_section_override",
                "material_description_override",
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
