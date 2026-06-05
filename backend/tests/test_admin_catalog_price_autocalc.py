from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.admin_service_catalog import (
    _autocalc_apply_for_services,
    _resolve_autocalc_service_ids,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Service,
    ServiceAuditLog,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_scope_data(db):
    cat_services = ServiceCategory(name="Услуги", is_active=True)
    cat_products = ServiceCategory(name="Заказ", is_active=True)
    db.add_all([cat_services, cat_products])
    db.flush()

    sub_braids = ServiceSubcategory(category_id=cat_services.id, name="Брейды", is_active=True)
    sub_curls = ServiceSubcategory(category_id=cat_services.id, name="Кудри", is_active=True)
    sub_hidden = ServiceSubcategory(category_id=cat_products.id, name="Скрытая", is_active=True)
    db.add_all([sub_braids, sub_curls, sub_hidden])
    db.flush()

    svc_a = Service(subcategory_id=sub_braids.id, name="A", is_active=True)
    svc_b = Service(subcategory_id=sub_curls.id, name="B", is_active=False)
    svc_hidden = Service(subcategory_id=sub_hidden.id, name="Hidden", is_active=True)
    db.add_all([svc_a, svc_b, svc_hidden])
    db.commit()
    return {
        "cat_services_id": int(cat_services.id),
        "sub_braids_id": int(sub_braids.id),
        "sub_curls_id": int(sub_curls.id),
        "allowed_ids": {int(svc_a.id), int(svc_b.id)},
        "hidden_id": int(svc_hidden.id),
    }


def test_resolve_scope_all_and_category(memory_db):
    seeded = _seed_scope_data(memory_db)
    all_ids = _resolve_autocalc_service_ids(
        memory_db,
        scope_mode="all",
        category_id=None,
        subcategory_id=None,
        service_ids=[],
    )
    assert set(all_ids) == seeded["allowed_ids"]

    cat_ids = _resolve_autocalc_service_ids(
        memory_db,
        scope_mode="category",
        category_id=seeded["cat_services_id"],
        subcategory_id=None,
        service_ids=[],
    )
    assert set(cat_ids) == seeded["allowed_ids"]


def test_resolve_scope_subcategory_and_services(memory_db):
    seeded = _seed_scope_data(memory_db)
    sub_ids = _resolve_autocalc_service_ids(
        memory_db,
        scope_mode="subcategory",
        category_id=seeded["cat_services_id"],
        subcategory_id=seeded["sub_braids_id"],
        service_ids=[],
    )
    assert len(sub_ids) == 1

    with pytest.raises(ValueError):
        _resolve_autocalc_service_ids(
            memory_db,
            scope_mode="services",
            category_id=None,
            subcategory_id=None,
            service_ids=[seeded["hidden_id"]],
        )


def test_apply_autocalc_rounding_and_null_skip(memory_db):
    u = User(
        username="super",
        display_name="Super",
        password_hash="x",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db = memory_db
    db.add(u)
    cat = ServiceCategory(name="Услуги", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Подкатегория", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Тест",
        is_active=True,
        price_junior_from=10.0,
        price_junior_to=20.0,
        price_middle_from=335.0,
        price_middle_to=None,
    )
    db.add(svc)
    db.commit()

    updated = _autocalc_apply_for_services(
        db,
        services=[svc],
        source_level="MIDDLE",
        target_level="JUNIOR",
        pct=50.0,
        changed_by_user_id=int(u.id),
    )
    db.commit()
    db.refresh(svc)

    assert updated == 1
    assert svc.price_junior_from == 168.0
    assert svc.price_junior_to == 20.0
    assert svc.updated_by_user_id == u.id
    assert isinstance(svc.updated_at, datetime)
    logs = list(db.query(ServiceAuditLog).filter(ServiceAuditLog.service_id == svc.id).all())
    assert logs
    assert any(l.field_name == "price_junior_from" for l in logs)

