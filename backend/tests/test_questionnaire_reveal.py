from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    CategoryQuestionnaireField,
    QuestionnaireFieldType,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    SubcategoryQuestionnaireField,
)
from app.questionnaire.answer_validate import validate_and_coerce_answers
from app.questionnaire.reveal import (
    RevealOnCheck,
    normalize_reveal_from_form,
    reveal_on_check_to_visibility_json,
)
from app.questionnaire.runtime_merge import load_merged_questionnaire_specs
from app.questionnaire_field_validate import validate_questionnaire_field_form


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_normalize_reveal_checkbox_only() -> None:
    rev, errs = normalize_reveal_from_form(
        field_type=QuestionnaireFieldType.TEXT,
        reveal_blocks=["kit"],
        reveal_field_keys_raw="",
    )
    assert errs
    assert rev.is_empty()


def test_validate_field_stores_reveal_json() -> None:
    n, errs = validate_questionnaire_field_form(
        field_key="need_kit",
        field_type_raw="CHECKBOX",
        label="Нужен комплект",
        required=False,
        placeholder=None,
        help_text=None,
        options_raw=None,
        min_raw=None,
        max_raw=None,
        reveal_blocks=["kit", "material_description"],
        reveal_field_keys="extra_note",
    )
    assert not errs and n is not None
    data = json.loads(n.visibility_json or "{}")
    assert data["reveal_on_check"]["blocks"] == ["kit", "material_description"]
    assert data["reveal_on_check"]["field_keys"] == ["extra_note"]


def test_material_kept_when_checkbox_can_reveal(db) -> None:
    cat = ServiceCategory(name="Вся голова", is_active=True)
    db.add(cat)
    db.flush()
    db.add(
        CategoryQuestionnaireField(
            category_id=cat.id,
            field_key="material_description",
            field_type=QuestionnaireFieldType.TEXTAREA,
            label="Описание про материал",
            required=False,
            sort_order=1,
        )
    )
    sub = ServiceSubcategory(
        category_id=cat.id,
        name="Безымянная",
        show_material_description=False,
        show_kit_section=False,
    )
    db.add(sub)
    db.flush()
    db.add(
        SubcategoryQuestionnaireField(
            subcategory_id=sub.id,
            field_key="need_kit",
            field_type=QuestionnaireFieldType.CHECKBOX,
            label="Нужен комплект",
            required=False,
            sort_order=0,
            visibility_json=reveal_on_check_to_visibility_json(
                RevealOnCheck(blocks=("kit", "material_description"), field_keys=())
            ),
        )
    )
    svc = Service(name="Тест", subcategory_id=sub.id, is_active=True)
    db.add(svc)
    db.commit()

    specs = load_merged_questionnaire_specs(db, int(svc.id))
    by_key = {s.field_key: s for s in specs}
    assert "material_description" in by_key
    assert by_key["material_description"].hidden_by_default is True
    assert by_key["need_kit"].reveal_on_check is not None
    assert "kit" in by_key["need_kit"].reveal_on_check.blocks

    answers, errs = validate_and_coerce_answers({"need_kit": "on", "material_description": "красный"}, specs)
    assert not errs
    assert answers["need_kit"] is True
    assert answers["material_description"] == "красный"

    answers2, errs2 = validate_and_coerce_answers({"material_description": "красный"}, specs)
    assert not errs2
    assert "material_description" not in answers2
