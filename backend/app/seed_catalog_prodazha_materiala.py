"""Сид «Продажа материала» (не в форме визита; этап 7 — отдельный поток продаж)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.seed_catalog_json import apply_service_catalog_from_json_path

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "prodazha_materiala_services.json"


def ensure_prodazha_materiala_catalog(db: Session) -> None:
    apply_service_catalog_from_json_path(db, _JSON_PATH)
