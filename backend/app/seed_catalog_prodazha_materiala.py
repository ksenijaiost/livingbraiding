"""Сид «Продажа материала» (не в форме визита; этап 7 — отдельный поток продаж)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Service, ServiceCategory, ServiceSubcategory
from app.seed_catalog_json import apply_service_catalog_from_json_path

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "prodazha_materiala_services.json"


def _apply_prodazha_materiala_retail_flags(db: Session) -> None:
    """Выставляет retail_material_* по подкатегории/названию (JSON пока без этих полей)."""
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Продажа материала"))
    if not cat:
        return
    subs = db.scalars(
        select(ServiceSubcategory).where(ServiceSubcategory.category_id == cat.id)
    ).all()
    for sub in subs:
        services = db.scalars(select(Service).where(Service.subcategory_id == sub.id)).all()
        sn = (sub.name or "").strip()
        sn_l = sn.lower()
        for svc in services:
            svc.retail_material_kanekalon = False
            svc.retail_material_kudri = False
            svc.retail_material_mix = False
            nm_l = (svc.name or "").lower()
            if "канекалон" in sn_l:
                svc.retail_material_kanekalon = True
            elif sn_l == "кудри":
                svc.retail_material_kudri = True
            elif sn_l == "другое":
                if "смешанный" in nm_l:
                    svc.retail_material_mix = True
                elif any(x in nm_l for x in ("развес", "канекалон", "изик")):
                    svc.retail_material_kanekalon = True


def ensure_prodazha_materiala_catalog(db: Session) -> None:
    apply_service_catalog_from_json_path(db, _JSON_PATH)
    _apply_prodazha_materiala_retail_flags(db)
