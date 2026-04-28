"""Сид категорий «Малышки 3-7л» и «Мужчины»: seed_data/malishki_muzhchiny_services.json."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.seed_catalog_json import apply_service_catalog_from_json_path

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "malishki_muzhchiny_services.json"


def ensure_malishki_muzhchiny_catalog(db: Session) -> None:
    apply_service_catalog_from_json_path(db, _JSON_PATH)
