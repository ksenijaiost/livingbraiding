"""
Общая загрузка каталога услуг из JSON (категория → подкатегории → услуги с price / junior|middle|senior).

Правило «до»: не задано / xx / x / хх (лат., кир.) / — → копируем «от».
Идемпотентность: услуга с тем же именем в подкатегории не пересоздаётся.

Опционально на уровне каталога: include_in_visit (bool) → ServiceCategory.include_in_visit
(например false для «Продажа материала»).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Service, ServiceCategory, ServiceSubcategory


def _normalize_from_to(from_val: Any, to_val: Any) -> tuple[float | None, float | None]:
    if from_val is None:
        return None, None
    f = float(from_val)
    if to_val is None:
        return f, f
    if isinstance(to_val, str):
        t = to_val.strip().lower()
        if t in ("", "xx", "x", "хх", "х", "-", "—"):
            return f, f
    return f, float(to_val)


def _parse_level(pr: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not pr:
        return None, None
    return _normalize_from_to(pr.get("from"), pr.get("to"))


def _resolve_level_prices(
    svc_raw: dict[str, Any], level_key: str, common: tuple[float | None, float | None]
) -> tuple[float | None, float | None]:
    if level_key in svc_raw and svc_raw[level_key] is not None:
        return _parse_level(svc_raw[level_key])
    return common


def _get_or_create_category(db: Session, name: str) -> ServiceCategory:
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == name))
    if cat:
        return cat
    cat = ServiceCategory(name=name)
    db.add(cat)
    db.flush()
    return cat


def _get_or_create_subcategory(db: Session, category_id: int, name: str) -> ServiceSubcategory:
    sub = db.scalar(
        select(ServiceSubcategory).where(
            ServiceSubcategory.category_id == category_id,
            ServiceSubcategory.name == name,
        )
    )
    if sub:
        return sub
    sub = ServiceSubcategory(category_id=category_id, name=name)
    db.add(sub)
    db.flush()
    return sub


def _ensure_service_row(
    db: Session,
    subcategory_id: int,
    name: str,
    *,
    price_junior_from: float | None,
    price_junior_to: float | None,
    price_middle_from: float | None,
    price_middle_to: float | None,
    price_senior_from: float | None,
    price_senior_to: float | None,
    is_active: bool = True,
    order_rubber_extra_time_amort: bool | None = None,
    master_pay_amount: float | None = None,
    studio_pay_amount: float | None = None,
    fixed_expense_amount: float | None = None,
    is_per_unit: bool | None = None,
    unit_label: str | None = None,
) -> None:
    existing = db.scalar(
        select(Service).where(Service.subcategory_id == subcategory_id, Service.name == name)
    )
    if existing:
        existing.is_active = bool(is_active)
        existing.price_junior_from = price_junior_from
        existing.price_junior_to = price_junior_to
        existing.price_middle_from = price_middle_from
        existing.price_middle_to = price_middle_to
        existing.price_senior_from = price_senior_from
        existing.price_senior_to = price_senior_to
        if order_rubber_extra_time_amort is not None:
            existing.order_rubber_extra_time_amort = bool(order_rubber_extra_time_amort)
        if master_pay_amount is not None:
            existing.master_pay_amount = float(master_pay_amount)
        if studio_pay_amount is not None:
            existing.studio_pay_amount = float(studio_pay_amount)
        if fixed_expense_amount is not None:
            existing.fixed_expense_amount = float(fixed_expense_amount)
        if is_per_unit is not None:
            existing.is_per_unit = bool(is_per_unit)
        if unit_label is not None:
            existing.unit_label = str(unit_label) if unit_label else None
        return
    db.add(
        Service(
            subcategory_id=subcategory_id,
            name=name,
            is_active=is_active,
            price_junior_from=price_junior_from,
            price_junior_to=price_junior_to,
            price_middle_from=price_middle_from,
            price_middle_to=price_middle_to,
            price_senior_from=price_senior_from,
            price_senior_to=price_senior_to,
            order_rubber_extra_time_amort=bool(order_rubber_extra_time_amort)
            if order_rubber_extra_time_amort is not None
            else False,
            master_pay_amount=float(master_pay_amount) if master_pay_amount is not None else None,
            studio_pay_amount=float(studio_pay_amount) if studio_pay_amount is not None else None,
            fixed_expense_amount=float(fixed_expense_amount) if fixed_expense_amount is not None else None,
            is_per_unit=bool(is_per_unit) if is_per_unit is not None else False,
            unit_label=str(unit_label) if unit_label else None,
        )
    )


def apply_service_catalog_from_dict(db: Session, data: dict[str, Any]) -> None:
    cat_name = str(data.get("category") or "").strip()
    if not cat_name:
        raise ValueError("В JSON каталога нужно поле category (непустая строка).")
    cat = _get_or_create_category(db, cat_name)
    inv = data.get("include_in_visit")
    if inv is not None:
        cat.include_in_visit = bool(inv)

    for sub_raw in data.get("subcategories") or []:
        if not isinstance(sub_raw, dict):
            continue
        sub_name = str(sub_raw.get("name") or "").strip()
        if not sub_name:
            continue
        sub = _get_or_create_subcategory(db, cat.id, sub_name)
        if "is_active" in sub_raw:
            sub.is_active = bool(sub_raw.get("is_active"))

        for svc_raw in sub_raw.get("services") or []:
            if not isinstance(svc_raw, dict):
                continue
            svc_name = str(svc_raw.get("name") or "").strip()
            if not svc_name:
                continue
            is_active = bool(svc_raw.get("is_active", True))

            common = _parse_level(svc_raw.get("price"))
            pjf, pjt = _resolve_level_prices(svc_raw, "junior", common)
            pmf, pmt = _resolve_level_prices(svc_raw, "middle", common)
            psf, pst = _resolve_level_prices(svc_raw, "senior", common)

            rubber_raw = svc_raw.get("order_rubber_extra_time_amort")
            rubber_opt = bool(rubber_raw) if rubber_raw is not None else None

            econ = svc_raw.get("economics") if isinstance(svc_raw.get("economics"), dict) else {}
            mp = econ.get("master_pay")
            sp = econ.get("studio_pay")
            fx = econ.get("fixed_expense")
            ipu = econ.get("is_per_unit")
            ul = econ.get("unit_label")

            _ensure_service_row(
                db,
                sub.id,
                svc_name,
                price_junior_from=pjf,
                price_junior_to=pjt,
                price_middle_from=pmf,
                price_middle_to=pmt,
                price_senior_from=psf,
                price_senior_to=pst,
                is_active=is_active,
                order_rubber_extra_time_amort=rubber_opt,
                master_pay_amount=mp,
                studio_pay_amount=sp,
                fixed_expense_amount=fx,
                is_per_unit=bool(ipu) if ipu is not None else None,
                unit_label=str(ul) if ul is not None else None,
            )


def apply_service_catalog_bundle(db: Session, data: dict[str, Any]) -> None:
    """
    Один каталог: { "category", "subcategories" }.
    Несколько: { "catalogs": [ { "category", "subcategories" }, ... ] }.
    """
    if "catalogs" in data:
        for part in data.get("catalogs") or []:
            if isinstance(part, dict):
                apply_service_catalog_from_dict(db, part)
        return
    apply_service_catalog_from_dict(db, data)


def apply_service_catalog_from_json_path(db: Session, path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Нет файла сида каталога: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    apply_service_catalog_bundle(db, data)
