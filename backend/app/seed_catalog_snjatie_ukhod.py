"""Сиды категорий «Снятие» и «Уход»: seed_data/snjatie_ukhod_services.json."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.seed_catalog_json import apply_service_catalog_from_json_path

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "snjatie_ukhod_services.json"


def ensure_snjatie_ukhod_catalogs(db: Session) -> None:
    apply_service_catalog_from_json_path(db, _JSON_PATH)
