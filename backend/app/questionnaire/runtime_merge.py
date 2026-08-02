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
from app.questionnaire.reveal import (
    REVEAL_BLOCK_MATERIAL,
    RevealOnCheck,
    field_keys_hidden_by_default,
    parse_reveal_on_check,
    specs_can_reveal_block,
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
    reveal_on_check: RevealOnCheck | None = None
    hidden_by_default: bool = False

    def to_client_json(self) -> dict:
        d: dict = {
            "field_key": self.field_key,
            "field_type": self.field_type.value,
            "label": self.label,
            "required": self.required,
            "placeholder": self.placeholder or "",
            "help_text": self.help_text or "",
            "options": self.options,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "hidden_by_default": bool(self.hidden_by_default),
        }
        if self.reveal_on_check and not self.reveal_on_check.is_empty():
            d["reveal_on_check"] = self.reveal_on_check.to_dict()
        return d


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


def _reveal_from_row(visibility_json: str | None) -> RevealOnCheck | None:
    rev = parse_reveal_on_check(visibility_json)
    return None if rev.is_empty() else rev


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
        reveal_on_check=_reveal_from_row(row.visibility_json),
    )


def _material_description_visible(service: Service, subcategory: ServiceSubcategory) -> bool:
    """Показывать поле material_description с учётом подкатегории и переопределения услуги."""
    md_override = getattr(service, "material_description_override", None)
    if md_override is True:
        return True
    if md_override is False:
        return False
    return bool(subcategory.show_material_description)


def _filter_category_specs(
    specs: list[MergedQuestionnaireFieldSpec],
    *,
    service: Service,
    subcategory: ServiceSubcategory,
    keep_material_for_reveal: bool,
) -> list[MergedQuestionnaireFieldSpec]:
    """Скрыть «Описание про материал» по флагам подкатегории и услуги."""
    if _material_description_visible(service, subcategory):
        return specs
    if keep_material_for_reveal:
        return specs
    return [s for s in specs if s.field_key != "material_description"]


def _drop_hidden_material_description(
    specs: list[MergedQuestionnaireFieldSpec],
    *,
    service: Service,
    subcategory: ServiceSubcategory,
    keep_material_for_reveal: bool,
) -> list[MergedQuestionnaireFieldSpec]:
    """Убрать material_description на любом уровне (категория/подкатегория/услуга)."""
    if _material_description_visible(service, subcategory) or keep_material_for_reveal:
        return specs
    return [s for s in specs if s.field_key != "material_description"]


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
        reveal_on_check=_reveal_from_row(row.visibility_json),
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
        reveal_on_check=_reveal_from_row(row.visibility_json),
    )


def _with_hidden_defaults(
    specs: list[MergedQuestionnaireFieldSpec],
    *,
    material_always_visible: bool,
) -> list[MergedQuestionnaireFieldSpec]:
    hidden = field_keys_hidden_by_default(specs)
    if material_always_visible:
        hidden.discard(REVEAL_BLOCK_MATERIAL)
    if not hidden:
        return specs
    out: list[MergedQuestionnaireFieldSpec] = []
    for s in specs:
        if s.field_key in hidden:
            out.append(
                MergedQuestionnaireFieldSpec(
                    field_key=s.field_key,
                    field_type=s.field_type,
                    label=s.label,
                    required=s.required,
                    placeholder=s.placeholder,
                    help_text=s.help_text,
                    options=s.options,
                    min_value=s.min_value,
                    max_value=s.max_value,
                    reveal_on_check=s.reveal_on_check,
                    hidden_by_default=True,
                )
            )
        else:
            out.append(s)
    return out


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

    # Предварительно: есть ли галочка, открывающая material_description
    probe = (
        [_spec_from_category_row(r) for r in cat_rows]
        + [_spec_from_subcat_row(r) for r in sub_rows]
        + [_spec_from_service_row(r) for r in svc_rows]
    )
    keep_material_for_reveal = specs_can_reveal_block(probe, REVEAL_BLOCK_MATERIAL)
    material_always_visible = _material_description_visible(svc, sub)

    if sub.show_thermo_visit:
        cat_specs: list[MergedQuestionnaireFieldSpec] = []
    else:
        cat_specs = [_spec_from_category_row(r) for r in cat_rows]
        cat_specs = _filter_category_specs(
            cat_specs,
            service=svc,
            subcategory=sub,
            keep_material_for_reveal=keep_material_for_reveal,
        )

    merged = _drop_hidden_material_description(
        cat_specs
        + [_spec_from_subcat_row(r) for r in sub_rows]
        + [_spec_from_service_row(r) for r in svc_rows],
        service=svc,
        subcategory=sub,
        keep_material_for_reveal=keep_material_for_reveal,
    )
    return _with_hidden_defaults(merged, material_always_visible=material_always_visible)


def merged_questionnaire_client_json(db: Session, service_id: int) -> list[dict]:
    return [s.to_client_json() for s in load_merged_questionnaire_specs(db, int(service_id))]
