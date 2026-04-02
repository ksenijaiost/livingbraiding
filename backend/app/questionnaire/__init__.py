"""
Анкета и структуры данных для деталей услуги (`visit_services.details_json`).

Расположение:
- `schemas.py` — Pydantic-модели (комплект + типовые поля услуги).
- `data/catalog/` — человекочитаемый каталог категорий/подкатегорий/услуг (для импорта в БД или подсказки в UI).
- `data/forms/` — описание полей формы по `service_code` (общие поля для нескольких услуг).
- `data/examples/` — примеры заполненных JSON (документация + тесты).
- `loader.py`, `registry.py` — чтение JSON и связь код услуги → файл формы.
"""

from app.questionnaire.schemas import (
    KitBlock,
    KitFromStock,
    KitNew,
    KitOwn,
    KitOwnExtra,
    VisitServiceDetailsPayload,
    parse_visit_service_details,
)
from app.questionnaire.loader import (
    load_catalog_slice,
    load_form_definition,
    questionnaire_data_dir,
)
from app.questionnaire.registry import SERVICE_FORM_FILES, form_file_for_service

__all__ = [
    "KitBlock",
    "KitFromStock",
    "KitNew",
    "KitOwn",
    "KitOwnExtra",
    "VisitServiceDetailsPayload",
    "parse_visit_service_details",
    "load_catalog_slice",
    "load_form_definition",
    "questionnaire_data_dir",
    "SERVICE_FORM_FILES",
    "form_file_for_service",
]
