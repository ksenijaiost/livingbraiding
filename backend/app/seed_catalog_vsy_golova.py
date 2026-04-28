"""
Сид категории «Вся голова»: данные в seed_data/vsy_golova_services.json.

См. также app.seed_catalog_json — общая логика загрузки.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.seed_catalog_json import apply_service_catalog_from_json_path

_JSON_PATH = Path(__file__).resolve().parent / "seed_data" / "vsy_golova_services.json"


def load_vsy_golova_definitions() -> dict[str, Any]:
    if not _JSON_PATH.is_file():
        raise FileNotFoundError(f"Нет файла сида каталога: {_JSON_PATH}")
    return json.loads(_JSON_PATH.read_text(encoding="utf-8"))


def ensure_vsy_golova_catalog(db: Session) -> None:
    apply_service_catalog_from_json_path(db, _JSON_PATH)
