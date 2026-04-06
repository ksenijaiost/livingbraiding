"""
Склейка полей анкеты: категория → подкатегория → услуга (порядок: sort_order, id).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    CategoryQuestionnaireField,
    QuestionnaireFieldType,
    Service,
    ServiceQuestionnaireField,
    ServiceSubcategory,
    SubcategoryQuestionnaireField,
)


@dataclass(frozen=True)
class MergedQuestionnaireFieldSpec:
    """Описание поля для JSON в каталоге и для серверной проверки ответов."""

    field_key: str
    field_type: QuestionnaireFieldType
    label: str
    required: bool
    placeholder: str | None
    help_text: str | None
    options: list[dict[str, str]]
    min_value: float | None
    max_value: float | None

    def to_client_json(self) -> dict:
        return {
            "field_key": self.field_key,
            "field_type": self.field_type.value,
            "label": self.label,
            "required": self.required,
            "placeholder": self.placeholder or "",
            "help_text": self.help_text or "",
            "options": self.options,
            "min_value": self.min_value,
            "max_value": self.max_value,
        }


def _options_from_stored(options_json: str | None) -> list[dict[str, str]]:
    if not options_json or not str(options_json).strip():
        return []
    try:
        data = json.loads(options_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if isinstance(item, dict) and "value" in item and "label" in item:
            out.append({"value": str(item["value"]), "label": str(item["label"])})
    return out


def _spec_from_category_row(row: CategoryQuestionnaireField) -> MergedQuestionnaireFieldSpec:
    return MergedQuestionnaireFieldSpec(
        field_key=row.field_key,
        field_type=row.field_type,
        label=row.label,
        required=row.required,
        placeholder=row.placeholder,
        help_text=row.help_text,
        options=_options_from_stored(row.options_json),
        min_value=row.min_value,
        max_value=row.max_value,
    )


def _filter_category_specs(
    specs: list[MergedQuestionnaireFieldSpec],
    *,
    service: Service,
    subcategory: ServiceSubcategory,
) -> list[MergedQuestionnaireFieldSpec]:
    """Скрыть «Описание про материал» по флагам подкатегории и услуги."""
    out: list[MergedQuestionnaireFieldSpec] = []
    for s in specs:
        if s.field_key == "material_description":
            if not subcategory.show_material_description:
                continue
            if service.hide_material_description:
                continue
        out.append(s)
    return out


def _spec_from_subcat_row(row: SubcategoryQuestionnaireField) -> MergedQuestionnaireFieldSpec:
    return MergedQuestionnaireFieldSpec(
        field_key=row.field_key,
        field_type=row.field_type,
        label=row.label,
        required=row.required,
        placeholder=row.placeholder,
        help_text=row.help_text,
        options=_options_from_stored(row.options_json),
        min_value=row.min_value,
        max_value=row.max_value,
    )


def _spec_from_service_row(row: ServiceQuestionnaireField) -> MergedQuestionnaireFieldSpec:
    return MergedQuestionnaireFieldSpec(
        field_key=row.field_key,
        field_type=row.field_type,
        label=row.label,
        required=row.required,
        placeholder=row.placeholder,
        help_text=row.help_text,
        options=_options_from_stored(row.options_json),
        min_value=row.min_value,
        max_value=row.max_value,
    )


def load_merged_questionnaire_specs(db: Session, service_id: int) -> list[MergedQuestionnaireFieldSpec]:
    svc = db.scalar(
        select(Service)
        .options(selectinload(Service.subcategory))
        .where(Service.id == service_id)
    )
    if svc is None or svc.subcategory is None:
        return []
    sub = svc.subcategory
    cat_id = sub.category_id
    sub_id = sub.id

    cat_rows = list(
        db.scalars(
            select(CategoryQuestionnaireField)
            .where(CategoryQuestionnaireField.category_id == cat_id)
            .order_by(CategoryQuestionnaireField.sort_order, CategoryQuestionnaireField.id)
        ).all()
    )
    cat_specs = [_spec_from_category_row(r) for r in cat_rows]
    cat_specs = _filter_category_specs(cat_specs, service=svc, subcategory=sub)

    sub_rows = list(
        db.scalars(
            select(SubcategoryQuestionnaireField)
            .where(SubcategoryQuestionnaireField.subcategory_id == sub_id)
            .order_by(SubcategoryQuestionnaireField.sort_order, SubcategoryQuestionnaireField.id)
        ).all()
    )
    svc_rows = list(
        db.scalars(
            select(ServiceQuestionnaireField)
            .where(ServiceQuestionnaireField.service_id == service_id)
            .order_by(ServiceQuestionnaireField.sort_order, ServiceQuestionnaireField.id)
        ).all()
    )
    return (
        cat_specs
        + [_spec_from_subcat_row(r) for r in sub_rows]
        + [_spec_from_service_row(r) for r in svc_rows]
    )


def merged_questionnaire_client_json(db: Session, service_id: int) -> list[dict]:
    return [s.to_client_json() for s in load_merged_questionnaire_specs(db, service_id)]
