"""
Сид категории «Вся голова»: подкатегории и услуги из JSON.

Правило цен при загрузке: если «до» не задано, пусто, «xx» / «x» — копируем «от» для этого уровня.
Идемпотентность: существующие услуги (та же подкатегория + имя) не перезаписываются — чтобы правки суперадмина не затирались при рестарте.

В каждой услуге можно задать общую пару цен: `"price": {"from": N, "to": M}` — она подставится во все три уровня (junior/middle/senior),
либо переопределить уровни полями `junior` / `middle` / `senior`.

Дополняйте `seed_data/vsy_golova_services.json` новыми подкатегориями и услугами по мере переноса со скринов;
остальные категории — отдельными JSON и вызовами по тому же шаблону (следующие порции).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Service, ServiceCategory, ServiceSubcategory

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "vsy_golova_services.json"


def _normalize_from_to(from_val: Any, to_val: Any) -> tuple[float | None, float | None]:
    if from_val is None:
        return None, None
    f = float(from_val)
    if to_val is None:
        return f, f
    if isinstance(to_val, str):
        t = to_val.strip().lower()
        if t in ("", "xx", "x", "-", "—"):
            return f, f
    return f, float(to_val)


def _parse_level(pr: dict[str, Any] | None) -> tuple[float | None, float | None]:
    if not pr:
        return None, None
    return _normalize_from_to(pr.get("from"), pr.get("to"))


def _resolve_level_prices(
    svc_raw: dict[str, Any], level_key: str, common: tuple[float | None, float | None]
) -> tuple[float | None, float | None]:
    """Явный `junior`/`middle`/`senior` перекрывает общий блок `price` (одна пара от/до на все уровни)."""
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
) -> None:
    exists = db.scalar(
        select(Service.id).where(Service.subcategory_id == subcategory_id, Service.name == name)
    )
    if exists:
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
        )
    )


def load_vsy_golova_definitions() -> dict[str, Any]:
    if not _JSON_PATH.is_file():
        raise FileNotFoundError(f"Нет файла сида каталога: {_JSON_PATH}")
    return json.loads(_JSON_PATH.read_text(encoding="utf-8"))


def ensure_vsy_golova_catalog(db: Session) -> None:
    data = load_vsy_golova_definitions()
    cat_name = str(data.get("category") or "Вся голова").strip() or "Вся голова"
    cat = _get_or_create_category(db, cat_name)

    for sub_raw in data.get("subcategories") or []:
        if not isinstance(sub_raw, dict):
            continue
        sub_name = str(sub_raw.get("name") or "").strip()
        if not sub_name:
            continue
        sub = _get_or_create_subcategory(db, cat.id, sub_name)

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
            )
