from __future__ import annotations

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
from app.questionnaire.runtime_merge import load_merged_questionnaire_specs


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


def _seed_service(db, *, show_material: bool, subcat_field: bool = False) -> Service:
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
        show_material_description=show_material,
        show_kit_section=False,
    )
    db.add(sub)
    db.flush()
    if subcat_field:
        db.add(
            SubcategoryQuestionnaireField(
                subcategory_id=sub.id,
                field_key="material_description",
                field_type=QuestionnaireFieldType.TEXTAREA,
                label="Описание про материал (подкатегория)",
                required=False,
                sort_order=1,
            )
        )
    svc = Service(name="Тест", subcategory_id=sub.id, is_active=True)
    db.add(svc)
    db.commit()
    return db.scalar(select(Service).where(Service.id == svc.id))


def test_material_description_hidden_when_subcategory_flag_off(db) -> None:
    svc = _seed_service(db, show_material=False)
    keys = [s.field_key for s in load_merged_questionnaire_specs(db, int(svc.id))]
    assert "material_description" not in keys


def test_material_description_hidden_from_subcategory_level_too(db) -> None:
    svc = _seed_service(db, show_material=False, subcat_field=True)
    keys = [s.field_key for s in load_merged_questionnaire_specs(db, int(svc.id))]
    assert "material_description" not in keys


def test_material_description_shown_when_subcategory_flag_on(db) -> None:
    svc = _seed_service(db, show_material=True)
    keys = [s.field_key for s in load_merged_questionnaire_specs(db, int(svc.id))]
    assert keys.count("material_description") == 1
