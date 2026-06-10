from __future__ import annotations

import json
from collections import defaultdict
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, require_role
from app.zakaz_blanks import section_from_kit_key
from app.db.models import CatalogProduct, Kit, KitBlankStock, Service, ServiceCategory, ServiceSubcategory, UserRole
from app.db.session import get_db
from app.forms_parse import parse_bool, parse_optional_float
from app.ru_labels import format_price_integer_rub
from app.webui import templates, ctx as _ctx


router = APIRouter()

_BLANK_CATALOG_CATEGORY = "Заказ"
_BLANK_CATALOG_SUBCATEGORY = "Заготовки поштучно"


def _parse_catalog_meta(raw: str | None) -> dict[str, Any]:
    try:
        meta = json.loads(raw or "{}")
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _kit_is_catalog_active(kit: Kit) -> bool:
    return bool(kit.is_active) and not bool(kit.is_archived)


def catalog_blank_kit_key_usage(db: Session, kit_key: str, *, limit: int = 12) -> dict[str, Any]:
    """Комплекты, в составе или остатках которых встречается kit_key."""
    kk = str(kit_key or "").strip()
    if not kk:
        return {"kit_key": "", "total": 0, "active": 0, "kits": [], "truncated": False}

    from app.kit_blank_stock_core import parse_composition_totals

    matched: dict[int, Kit] = {}
    stock_kit_ids = {
        int(x)
        for x in db.scalars(select(KitBlankStock.kit_id).where(KitBlankStock.kit_key == kk)).all()
        if int(x) > 0
    }
    for kit in db.scalars(select(Kit)).all():
        if int(kit.id) in stock_kit_ids or kk in parse_composition_totals(kit):
            matched[int(kit.id)] = kit

    kits_sorted = sorted(
        matched.values(),
        key=lambda k: (not _kit_is_catalog_active(k), str(k.sku or ""), int(k.id)),
    )
    active_count = sum(1 for k in matched.values() if _kit_is_catalog_active(k))
    total = len(matched)
    kits_out: list[dict[str, Any]] = []
    for kit in kits_sorted[: max(0, int(limit))]:
        kits_out.append(
            {
                "id": int(kit.id),
                "sku": str(kit.sku or ""),
                "title": str(kit.title or ""),
                "is_active": bool(kit.is_active),
                "is_archived": bool(kit.is_archived),
                "is_catalog_active": _kit_is_catalog_active(kit),
            }
        )
    return {
        "kit_key": kk,
        "total": total,
        "active": active_count,
        "kits": kits_out,
        "truncated": total > len(kits_out),
    }


def _catalog_blank_kit_key_owner_id(db: Session, kit_key: str, *, exclude_id: int | None = None) -> int | None:
    kk = str(kit_key or "").strip()
    if not kk:
        return None
    rows = db.scalars(
        select(CatalogProduct).where(
            CatalogProduct.category_name == _BLANK_CATALOG_CATEGORY,
            CatalogProduct.subcategory_name == _BLANK_CATALOG_SUBCATEGORY,
        )
    ).all()
    for row in rows:
        if exclude_id is not None and int(row.id) == int(exclude_id):
            continue
        existing = str(_parse_catalog_meta(row.meta_json).get("kit_key") or "").strip()
        if existing == kk:
            return int(row.id)
    return None


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
                .where(CatalogProduct.category_name == selected)
                .order_by(CatalogProduct.subcategory_name.asc(), CatalogProduct.sort_order.asc(), CatalogProduct.name.asc())
            ).all()
        )
        if product_rows:
            is_catalog_products_category = True
            groups_active: dict[str, list[SimpleNamespace]] = defaultdict(list)
            groups_inactive: dict[str, list[SimpleNamespace]] = defaultdict(list)

            def _catalog_row_ns(row: CatalogProduct) -> SimpleNamespace:
                try:
                    meta = json.loads(row.meta_json or "{}")
                except Exception:
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                return SimpleNamespace(
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
                    is_bu=bool(meta.get("is_bu")),
                    is_active=row.is_active,
                )

            for row in product_rows:
                bucket = groups_active if row.is_active else groups_inactive
                bucket[row.subcategory_name].append(_catalog_row_ns(row))
            sub_names = sorted(set(groups_active.keys()) | set(groups_inactive.keys()))
            grouped_rows = [
                SimpleNamespace(
                    subcategory_name=sub_name,
                    rows=groups_active.get(sub_name, []),
                    inactive_rows=groups_inactive.get(sub_name, []),
                )
                for sub_name in sub_names
            ]
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
            grouped_rows = [
                SimpleNamespace(subcategory_name=sub_name, rows=groups2[sub_name], inactive_rows=[])
                for sub_name in sorted(groups2.keys())
            ]
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
    if row.category_name == "Заказ" and row.subcategory_name == "Заготовки поштучно":
        new_name = str(form.get("name") or "").strip()
        kit_key = str(meta.get("kit_key") or "").strip()
        if not new_name or not kit_key:
            return _redirect("err", "blank_fields")
        if new_name != row.name:
            exists_id = db.scalar(
                select(CatalogProduct.id).where(
                    CatalogProduct.category_name == row.category_name,
                    CatalogProduct.subcategory_name == row.subcategory_name,
                    CatalogProduct.name == new_name,
                    CatalogProduct.id != row.id,
                )
            )
            if exists_id:
                return _redirect("err", "duplicate")
            row.name = new_name[:200]
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

    if category == _BLANK_CATALOG_CATEGORY and subcategory_name == _BLANK_CATALOG_SUBCATEGORY:
        kit_key_new = str(form.get("kit_key") or "").strip()
        if not kit_key_new:
            return _redirect("err", "blank_fields")
        if section_from_kit_key(kit_key_new) is None:
            return _redirect("err", "bad_kit_key_prefix")
        if _catalog_blank_kit_key_owner_id(db, kit_key_new) is not None:
            return _redirect("err", "duplicate_kit_key")

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
        "is_bu": False,
    }
    if category == _BLANK_CATALOG_CATEGORY and subcategory_name == _BLANK_CATALOG_SUBCATEGORY:
        meta["kit_key"] = kit_key_new[:80]
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


@router.get("/products-catalog/{row_id}/delete-preview")
def products_catalog_row_delete_preview(
    row_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
) -> JSONResponse:
    row = db.get(CatalogProduct, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Позиция прайса не найдена")
    meta = _parse_catalog_meta(row.meta_json)
    kk = str(meta.get("kit_key") or "").strip()
    applicable = (
        row.category_name == _BLANK_CATALOG_CATEGORY
        and row.subcategory_name == _BLANK_CATALOG_SUBCATEGORY
        and bool(kk)
    )
    if not applicable:
        return JSONResponse(
            {
                "applicable": False,
                "kit_key": kk,
                "total": 0,
                "active": 0,
                "kits": [],
                "truncated": False,
            }
        )
    usage = catalog_blank_kit_key_usage(db, kk)
    usage["applicable"] = True
    return JSONResponse(usage)


@router.post("/products-catalog/{row_id}/delete")
def products_catalog_row_delete(
    row_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    row = db.get(CatalogProduct, row_id)
    category = row.category_name if row else ""
    if row is None:
        raise HTTPException(status_code=404, detail="Позиция прайса не найдена")

    def _redirect(message_key: str, value: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"/products-catalog?{urlencode({'category': category, message_key: value})}",
            status_code=303,
        )

    if row.is_active:
        return _redirect("err", "delete_active")
    db.delete(row)
    db.commit()
    return _redirect("msg", "deleted")


@router.get("/price/products")
def products_catalog_view_legacy(category: str | None = None):
    url = "/products-catalog"
    if category and str(category).strip():
        url = f"/products-catalog?{urlencode({'category': str(category).strip()})}"
    return RedirectResponse(url=url, status_code=302)

