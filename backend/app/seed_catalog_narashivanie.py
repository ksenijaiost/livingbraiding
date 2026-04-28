"""Сид категории «Наращивание»: seed_data/narashivanie_services.json."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ServiceCategory, ServiceSubcategory
from app.seed_catalog_json import apply_service_catalog_from_json_path

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "narashivanie_services.json"


def ensure_narashivanie_catalog(db: Session) -> None:
    cat = db.scalar(select(ServiceCategory).where(ServiceCategory.name == "Наращивание"))
    if cat:
        old_led = db.scalar(
            select(ServiceSubcategory).where(
                ServiceSubcategory.category_id == cat.id,
                ServiceSubcategory.name == "led",
            )
        )
        if old_led:
            has_led_upper = db.scalar(
                select(ServiceSubcategory.id).where(
                    ServiceSubcategory.category_id == cat.id,
                    ServiceSubcategory.name == "LED",
                )
            )
            if has_led_upper is None:
                old_led.name = "LED"
                db.flush()
    apply_service_catalog_from_json_path(db, _JSON_PATH)
