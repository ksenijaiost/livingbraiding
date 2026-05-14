from __future__ import annotations

import json
from collections import defaultdict
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.db.models import CatalogProduct, Service, ServiceCategory, ServiceSubcategory, UserRole
from app.db.session import get_db
from app.forms_parse import parse_bool, parse_optional_float
from app.ru_labels import format_price_integer_rub
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _format_product_catalog_price(s: Service) -> str | None:
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


@router.get("/products-catalog", response_class=HTMLResponse)
def products_catalog_view(
    request: Request,
    category: str | None = None,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    # Исторически сюда попадали категории "товаров" из services.
    # Сейчас опираемся на имена отдельных потоков.
    _SERVICE_PRODUCT_CATS = ("Заказ", "Продажа материала")
    service_cats = list(
        db.scalars(
            select(ServiceCategory.name)
            .where(ServiceCategory.is_active.is_(True), ServiceCategory.name.in_(_SERVICE_PRODUCT_CATS))
            .order_by(ServiceCategory.name.asc())
        ).all()
    )
    product_cats = list(
        db.scalars(
            select(CatalogProduct.category_name)
            .where(CatalogProduct.is_active.is_(True))
            .distinct()
            .order_by(CatalogProduct.category_name.asc())
        ).all()
    )
    cats = sorted(set(service_cats + product_cats))
    selected = (category or "").strip() or (cats[0] if cats else None)
    grouped_rows: list[SimpleNamespace] = []
    is_catalog_products_category = False
    if selected:
        product_rows = list(
            db.scalars(
                select(CatalogProduct)
                .where(CatalogProduct.category_name == selected, CatalogProduct.is_active.is_(True))
                .order_by(CatalogProduct.subcategory_name.asc(), CatalogProduct.sort_order.asc(), CatalogProduct.name.asc())
            ).all()
        )
        if product_rows:
            is_catalog_products_category = True
            groups: dict[str, list[SimpleNamespace]] = defaultdict(list)
            for row in product_rows:
                try:
                    meta = json.loads(row.meta_json or "{}")
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                groups[row.subcategory_name].append(
                    SimpleNamespace(
                        id=row.id,
                        category_name=row.category_name,
                        subcategory_name=row.subcategory_name,
                        name=row.name,
                        price=row.price,
                        master_pay=float(meta.get("master_pay")) if meta.get("master_pay") is not None else None,
                        fixed_expense=float(meta.get("fixed_expense")) if meta.get("fixed_expense") is not None else None,
                        kit_key=(str(meta.get("kit_key") or "").strip() or None),
                        ignore_in_calc=bool(meta.get("ignore_in_calc") or False),
                        is_used_in_kit_form=bool(meta.get("is_used_in_kit_form") or False),
                        is_bu=bool(meta.get("is_bu") or ("Б/У" in row.name)),
                        is_active=row.is_active,
                    )
                )
            grouped_rows = [SimpleNamespace(subcategory_name=sub_name, rows=groups[sub_name]) for sub_name in sorted(groups.keys())]
        else:
            services = list(
                db.scalars(
                    select(Service)
                    .options(selectinload(Service.subcategory))
                    .join(ServiceSubcategory, Service.subcategory_id == ServiceSubcategory.id)
                    .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
                    .where(
                        ServiceCategory.name == selected,
                        ServiceCategory.is_active.is_(True),
                        ServiceCategory.name.in_(_SERVICE_PRODUCT_CATS),
                        ServiceSubcategory.is_active.is_(True),
                        Service.is_active.is_(True),
                    )
                    .order_by(ServiceSubcategory.name.asc(), Service.is_active.desc(), Service.name.asc())
                ).all()
            )
            groups2: dict[str, list[SimpleNamespace]] = defaultdict(list)
            for s in services:
                sub = s.subcategory
                groups2[sub.name if sub else "—"].append(
                    SimpleNamespace(
                        id=None,
                        category_name=selected,
                        subcategory_name=sub.name if sub else "—",
                        name=s.name,
                        price=_format_product_catalog_price(s),
                        master_pay=None,
                        fixed_expense=None,
                        kit_key=None,
                        ignore_in_calc=False,
                        is_used_in_kit_form=False,
                        is_bu=False,
                        is_active=s.is_active,
                    )
                )
            grouped_rows = [SimpleNamespace(subcategory_name=sub_name, rows=groups2[sub_name]) for sub_name in sorted(groups2.keys())]
    return templates.TemplateResponse(
        "products_catalog_view.html",
        _ctx(
            request,
            current_user=current_user,
            categories=cats,
            selected_category=selected,
            grouped_rows=grouped_rows,
            is_catalog_products_category=is_catalog_products_category,
            msg=msg,
            err=err,
        ),
    )


@router.post("/products-catalog/{row_id}/edit")
async def products_catalog_row_edit(
    row_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    row = db.get(CatalogProduct, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Позиция прайса не найдена")
    form = await request.form()
    category = (str(form.get("category") or "").strip() or row.category_name)

    def _redirect(message_key: str, value: str) -> RedirectResponse:
        return RedirectResponse(url=f"/products-catalog?{urlencode({'category': category, message_key: value})}", status_code=303)

    try:
        price = parse_optional_float(form.get("price"), field_name="price")
        master_pay = parse_optional_float(form.get("master_pay"), field_name="master_pay")
        fixed_expense = parse_optional_float(form.get("fixed_expense"), field_name="fixed_expense")
    except ValueError:
        return _redirect("err", "bad_price")

    try:
        meta = json.loads(row.meta_json or "{}")
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    row.price = price
    meta["master_pay"] = master_pay
    meta["fixed_expense"] = fixed_expense
    meta["is_used_in_kit_form"] = parse_bool(form.get("is_used_in_kit_form"))
    meta["ignore_in_calc"] = parse_bool(form.get("ignore_in_calc"))
    meta["is_bu"] = parse_bool(form.get("is_bu"))
    if row.category_name == "Заказ" and row.subcategory_name == "Заготовки поштучно":
        kk = str(form.get("kit_key") or "").strip()
        if kk:
            meta["kit_key"] = kk[:80]
        else:
            meta.pop("kit_key", None)
    row.is_active = parse_bool(form.get("is_active"))
    row.meta_json = json.dumps(meta, ensure_ascii=False)
    db.commit()
    return _redirect("msg", "saved")


@router.post("/products-catalog/new")
async def products_catalog_row_new(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    category = (str(form.get("category") or "").strip() or "")
    subcategory_name = (str(form.get("subcategory_name") or "").strip() or "")
    name = (str(form.get("name") or "").strip() or "")

    def _redirect(message_key: str, value: str) -> RedirectResponse:
        return RedirectResponse(url=f"/products-catalog?{urlencode({'category': category, message_key: value})}", status_code=303)

    if not category or not subcategory_name or not name:
        return _redirect("err", "empty")

    try:
        price = parse_optional_float(form.get("price"), field_name="price")
        master_pay = parse_optional_float(form.get("master_pay"), field_name="master_pay")
        fixed_expense = parse_optional_float(form.get("fixed_expense"), field_name="fixed_expense")
    except ValueError:
        return _redirect("err", "bad_price")

    exists_id = db.scalar(
        select(CatalogProduct.id).where(
            CatalogProduct.category_name == category,
            CatalogProduct.subcategory_name == subcategory_name,
            CatalogProduct.name == name,
        )
    )
    if exists_id:
        return _redirect("err", "duplicate")

    max_sort = db.scalar(
        select(func.max(CatalogProduct.sort_order)).where(
            CatalogProduct.category_name == category,
            CatalogProduct.subcategory_name == subcategory_name,
        )
    )
    meta = {
        "master_pay": master_pay,
        "fixed_expense": fixed_expense,
        "is_used_in_kit_form": parse_bool(form.get("is_used_in_kit_form")),
        "ignore_in_calc": parse_bool(form.get("ignore_in_calc")),
        "is_bu": parse_bool(form.get("is_bu")),
    }
    if category == "Заказ" and subcategory_name == "Заготовки поштучно":
        kk = str(form.get("kit_key") or "").strip()
        if kk:
            meta["kit_key"] = kk[:80]
    db.add(
        CatalogProduct(
            category_name=category,
            subcategory_name=subcategory_name,
            name=name,
            price=price,
            meta_json=json.dumps(meta, ensure_ascii=False),
            sort_order=int(max_sort or 0) + 1,
            is_active=parse_bool(form.get("is_active")),
        )
    )
    db.commit()
    return _redirect("msg", "saved")


@router.get("/price/products")
def products_catalog_view_legacy(category: str | None = None):
    url = "/products-catalog"
    if category and str(category).strip():
        url = f"/products-catalog?{urlencode({'category': str(category).strip()})}"
    return RedirectResponse(url=url, status_code=302)

