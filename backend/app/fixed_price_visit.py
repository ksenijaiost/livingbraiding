"""Фикс-цена в визите: прайс «Работа по фикс цене» и зеркальные услуги для выбора."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    CatalogProduct,
    ConsultationKind,
    Service,
    ServiceCategory,
    ServiceSubcategory,
)

FIXED_PRICE_VISIT_CATEGORY = "Работа по фикс цене"
FIXED_PRICE_VISIT_SUBCATEGORY = "—"
FIXED_PRICE_MIRROR_META_KEY = "mirror_service_id"
FIXED_PRICE_DURATION_MINUTES = 30


def is_fixed_price_category_name(name: str | None) -> bool:
    return (name or "").strip() == FIXED_PRICE_VISIT_CATEGORY


def is_fixed_price_visit_service(service: Service | None) -> bool:
    if service is None:
        return False
    sub = getattr(service, "subcategory", None)
    cat = getattr(sub, "category", None) if sub else None
    return is_fixed_price_category_name(getattr(cat, "name", None))


def normalize_fixed_price_qty(qty: int | None) -> int:
    try:
        n = int(qty or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n >= 1 else 1


def parse_catalog_meta(raw: str | None) -> dict[str, Any]:
    try:
        meta = json.loads(raw or "{}")
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def unit_economics(row: CatalogProduct) -> tuple[float, float, float]:
    """Цена / Работа / Расход за 1 шт."""
    meta = parse_catalog_meta(row.meta_json)
    price = float(row.price or 0)
    try:
        master = float(meta["master_pay"]) if meta.get("master_pay") is not None else 0.0
    except (TypeError, ValueError):
        master = 0.0
    try:
        expense = float(meta["fixed_expense"]) if meta.get("fixed_expense") is not None else 0.0
    except (TypeError, ValueError):
        expense = 0.0
    return price, master, expense


def ensure_fixed_price_visit_nodes(db: Session) -> bool:
    """Категория + служебная подкатегория для зеркальных услуг. True, если что-то создали/починили."""
    changed = False
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == FIXED_PRICE_VISIT_CATEGORY))
    if cat is None:
        cat = ServiceCategory(
            name=FIXED_PRICE_VISIT_CATEGORY,
            is_active=True,
            consultation_kind=ConsultationKind.OTHER,
        )
        db.add(cat)
        db.flush()
        changed = True
    else:
        if not cat.is_active:
            cat.is_active = True
            changed = True
        if cat.consultation_kind != ConsultationKind.OTHER:
            cat.consultation_kind = ConsultationKind.OTHER
            changed = True

    sub = db.scalar(
        select(ServiceSubcategory).where(
            ServiceSubcategory.category_id == cat.id,
            ServiceSubcategory.name == FIXED_PRICE_VISIT_SUBCATEGORY,
        )
    )
    if sub is None:
        sub = ServiceSubcategory(
            category_id=cat.id,
            name=FIXED_PRICE_VISIT_SUBCATEGORY,
            is_active=True,
            show_kit_section=False,
            show_tail_section=False,
            show_thermo_visit=False,
            show_material_description=False,
        )
        db.add(sub)
        db.flush()
        changed = True
    else:
        if not sub.is_active:
            sub.is_active = True
            changed = True
        if sub.show_kit_section or sub.show_tail_section or sub.show_thermo_visit:
            sub.show_kit_section = False
            sub.show_tail_section = False
            sub.show_thermo_visit = False
            changed = True
    return changed


def _mirror_subcategory(db: Session) -> ServiceSubcategory:
    ensure_fixed_price_visit_nodes(db)
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == FIXED_PRICE_VISIT_CATEGORY))
    assert cat is not None
    sub = db.scalar(
        select(ServiceSubcategory).where(
            ServiceSubcategory.category_id == cat.id,
            ServiceSubcategory.name == FIXED_PRICE_VISIT_SUBCATEGORY,
        )
    )
    assert sub is not None
    return sub


def _apply_mirror_prices(svc: Service, price: float | None) -> None:
    for attr in (
        "price_junior_from",
        "price_junior_to",
        "price_middle_from",
        "price_middle_to",
        "price_senior_from",
        "price_senior_to",
    ):
        setattr(svc, attr, price)


def sync_fixed_price_catalog_product(db: Session, row: CatalogProduct) -> Service | None:
    """Создать/обновить зеркальную услугу. Не трогает чужие категории."""
    if not is_fixed_price_category_name(row.category_name):
        return None
    sub = _mirror_subcategory(db)
    meta = parse_catalog_meta(row.meta_json)
    sid = 0
    try:
        sid = int(meta.get(FIXED_PRICE_MIRROR_META_KEY) or 0)
    except (TypeError, ValueError):
        sid = 0
    svc = db.get(Service, sid) if sid > 0 else None
    if svc is not None and int(svc.subcategory_id) != int(sub.id):
        svc = None
    if svc is None:
        svc = db.scalar(
            select(Service).where(Service.subcategory_id == sub.id, Service.name == row.name)
        )
    if svc is None:
        svc = Service(
            subcategory_id=sub.id,
            name=row.name,
            is_active=bool(row.is_active),
            estimated_duration_minutes=FIXED_PRICE_DURATION_MINUTES,
            kit_section_override=False,
            tail_section_override=False,
        )
        db.add(svc)
        db.flush()
    else:
        clash_id = db.scalar(
            select(Service.id).where(
                Service.subcategory_id == sub.id,
                Service.name == row.name,
                Service.id != svc.id,
            )
        )
        if clash_id is None:
            svc.name = row.name
    svc.is_active = bool(row.is_active)
    svc.kit_section_override = False
    svc.tail_section_override = False
    svc.estimated_duration_minutes = FIXED_PRICE_DURATION_MINUTES
    _apply_mirror_prices(svc, row.price)
    meta[FIXED_PRICE_MIRROR_META_KEY] = int(svc.id)
    row.meta_json = json.dumps(meta, ensure_ascii=False)
    return svc


def deactivate_fixed_price_mirror_service(db: Session, row: CatalogProduct) -> None:
    if not is_fixed_price_category_name(row.category_name):
        return
    meta = parse_catalog_meta(row.meta_json)
    sid = 0
    try:
        sid = int(meta.get(FIXED_PRICE_MIRROR_META_KEY) or 0)
    except (TypeError, ValueError):
        sid = 0
    svc = db.get(Service, sid) if sid > 0 else None
    if svc is None:
        sub = db.scalar(
            select(ServiceSubcategory)
            .join(ServiceCategory, ServiceSubcategory.category_id == ServiceCategory.id)
            .where(
                ServiceCategory.name == FIXED_PRICE_VISIT_CATEGORY,
                ServiceSubcategory.name == FIXED_PRICE_VISIT_SUBCATEGORY,
            )
        )
        if sub is not None:
            svc = db.scalar(
                select(Service).where(Service.subcategory_id == sub.id, Service.name == row.name)
            )
    if svc is not None:
        svc.is_active = False


def catalog_product_for_visit_service(db: Session, service: Service) -> CatalogProduct | None:
    if not is_fixed_price_visit_service(service):
        return None
    sid = int(service.id)
    rows = list(
        db.scalars(
            select(CatalogProduct).where(CatalogProduct.category_name == FIXED_PRICE_VISIT_CATEGORY)
        ).all()
    )
    for row in rows:
        meta = parse_catalog_meta(row.meta_json)
        try:
            if int(meta.get(FIXED_PRICE_MIRROR_META_KEY) or 0) == sid:
                return row
        except (TypeError, ValueError):
            continue
    name = (service.name or "").strip()
    for row in rows:
        if (row.name or "").strip() == name:
            return row
    return None


def economics_by_mirror_service_id(db: Session) -> dict[int, dict[str, float | None]]:
    """service_id → client_price / master_pay / fixed_expense (для JSON каталога визита)."""
    out: dict[int, dict[str, float | None]] = {}
    rows = list(
        db.scalars(
            select(CatalogProduct).where(CatalogProduct.category_name == FIXED_PRICE_VISIT_CATEGORY)
        ).all()
    )
    for row in rows:
        meta = parse_catalog_meta(row.meta_json)
        try:
            sid = int(meta.get(FIXED_PRICE_MIRROR_META_KEY) or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid <= 0:
            continue
        price, master, expense = unit_economics(row)
        out[sid] = {
            "client_price": None if row.price is None else price,
            "master_pay": None if meta.get("master_pay") is None else master,
            "fixed_expense": None if meta.get("fixed_expense") is None else expense,
        }
    return out


def is_fixed_price_service_id(db: Session, service_id: int) -> bool:
    if int(service_id or 0) <= 0:
        return False
    svc = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory).selectinload(ServiceSubcategory.category))
        .where(Service.id == int(service_id))
    )
    return is_fixed_price_visit_service(svc)
