"""
Загрузка JSON из `app/questionnaire/data/` (каталоги, формы, примеры).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def questionnaire_data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_catalog_slice(relative_path: str) -> dict[str, Any]:
    """
    relative_path: например \"catalog/v1/full_head_vpletenie_komplekta.json\"
    """
    path = questionnaire_data_dir() / relative_path
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался объект JSON: {path}")
    return data


def load_form_definition(relative_path: str) -> dict[str, Any]:
    path = questionnaire_data_dir() / relative_path
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался объект JSON: {path}")
    return data
