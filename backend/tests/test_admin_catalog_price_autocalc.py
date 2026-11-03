from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.admin_service_catalog import (
    _autocalc_apply_for_services,
    _load_autocalc_prefs,
    _parse_autocalc_discount,
    _resolve_autocalc_service_ids,
    _save_autocalc_prefs,
)
from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Service,
    ServiceAuditLog,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    WorkRate,
)
from app.db.session import get_db
from app.main import app
from app.work_rate_keys import PRICE_AUTOCALC_BASE_LEVEL, PRICE_AUTOCALC_DISCOUNTS


@pytest.fixture()
def memory_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _auth(user: User) -> AuthUser:
    return AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        roles=(user.role,),
        master_level=user.master_level,
    )


def _seed_user(db, *, username: str, role: UserRole) -> User:
    u = User(
        username=username,
        display_name=username,
        password_hash="x",
        role=role,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=role))
    db.commit()
    db.refresh(u)
    return u


def _client_for(db, user: User) -> TestClient:
    auth = _auth(user)

    def _override_user():
        return auth

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


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


def test_parse_autocalc_discount_range() -> None:
    assert _parse_autocalc_discount("") is None
    assert _parse_autocalc_discount("  ") is None
    assert _parse_autocalc_discount("20") == 20.0
    assert _parse_autocalc_discount("0") == 0.0
    assert _parse_autocalc_discount("100") == 100.0
    with pytest.raises(ValueError):
        _parse_autocalc_discount("101")
    with pytest.raises(ValueError):
        _parse_autocalc_discount("-1")


def test_apply_from_senior_discounts(memory_db):
    u = _seed_user(memory_db, username="super", role=UserRole.ADMIN_SUPER)
    db = memory_db
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
        price_senior_from=3000.0,
        price_senior_to=4000.0,
        price_middle_from=1.0,
        price_middle_to=2.0,
        price_junior_from=3.0,
        price_junior_to=4.0,
    )
    db.add(svc)
    db.commit()

    updated = _autocalc_apply_for_services(
        db,
        services=[svc],
        base_level="SENIOR",
        discounts={"MIDDLE": 20.0, "JUNIOR": 40.0},
        changed_by_user_id=int(u.id),
    )
    db.commit()
    db.refresh(svc)

    assert updated == 1
    assert svc.price_senior_from == 3000.0
    assert svc.price_senior_to == 4000.0
    assert svc.price_middle_from == 2400.0
    assert svc.price_middle_to == 3200.0
    assert svc.price_junior_from == 1800.0
    assert svc.price_junior_to == 2400.0
    logs = list(db.query(ServiceAuditLog).filter(ServiceAuditLog.service_id == svc.id).all())
    assert logs


def test_apply_from_middle_leaves_middle(memory_db):
    u = _seed_user(memory_db, username="super2", role=UserRole.ADMIN_SUPER)
    db = memory_db
    cat = ServiceCategory(name="Услуги", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Тест",
        is_active=True,
        price_middle_from=2000.0,
        price_middle_to=3000.0,
        price_senior_from=10.0,
        price_senior_to=20.0,
        price_junior_from=30.0,
        price_junior_to=40.0,
    )
    db.add(svc)
    db.commit()

    _autocalc_apply_for_services(
        db,
        services=[svc],
        base_level="MIDDLE",
        discounts={"SENIOR": 0.0, "JUNIOR": 50.0},
        changed_by_user_id=int(u.id),
    )
    db.commit()
    db.refresh(svc)
    assert svc.price_middle_from == 2000.0
    assert svc.price_middle_to == 3000.0
    assert svc.price_senior_from == 2000.0
    assert svc.price_senior_to == 3000.0
    assert svc.price_junior_from == 1000.0
    assert svc.price_junior_to == 1500.0


def test_null_base_does_not_overwrite_target(memory_db):
    u = _seed_user(memory_db, username="super3", role=UserRole.ADMIN_SUPER)
    db = memory_db
    cat = ServiceCategory(name="Услуги", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Тест",
        is_active=True,
        price_senior_from=3000.0,
        price_senior_to=None,
        price_junior_from=111.0,
        price_junior_to=222.0,
    )
    db.add(svc)
    db.commit()

    updated = _autocalc_apply_for_services(
        db,
        services=[svc],
        base_level="SENIOR",
        discounts={"JUNIOR": 40.0},
        changed_by_user_id=int(u.id),
    )
    db.commit()
    db.refresh(svc)
    assert updated == 1
    assert svc.price_junior_from == 1800.0
    assert svc.price_junior_to == 222.0


def test_empty_discount_level_not_changed(memory_db):
    u = _seed_user(memory_db, username="super4", role=UserRole.ADMIN_SUPER)
    db = memory_db
    cat = ServiceCategory(name="Услуги", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Тест",
        is_active=True,
        price_senior_from=3000.0,
        price_senior_to=4000.0,
        price_middle_from=111.0,
        price_middle_to=222.0,
        price_junior_from=333.0,
        price_junior_to=444.0,
    )
    db.add(svc)
    db.commit()

    _autocalc_apply_for_services(
        db,
        services=[svc],
        base_level="SENIOR",
        discounts={"JUNIOR": 40.0},
        changed_by_user_id=int(u.id),
    )
    db.commit()
    db.refresh(svc)
    assert svc.price_middle_from == 111.0
    assert svc.price_middle_to == 222.0
    assert svc.price_junior_from == 1800.0
    assert svc.price_junior_to == 2400.0


def test_prefs_saved_and_loaded(memory_db):
    u = _seed_user(memory_db, username="super5", role=UserRole.ADMIN_SUPER)
    db = memory_db
    base, discounts = _load_autocalc_prefs(db)
    assert base == "SENIOR"
    assert discounts == {"MIDDLE": 20.0, "JUNIOR": 40.0}

    _save_autocalc_prefs(
        db,
        base_level="MIDDLE",
        discounts={"JUNIOR": 15.0, "SENIOR": 5.0},
        user_id=int(u.id),
    )
    db.commit()
    base2, discounts2 = _load_autocalc_prefs(db)
    assert base2 == "MIDDLE"
    assert discounts2 == {"JUNIOR": 15.0, "SENIOR": 5.0}
    assert db.scalar(select(WorkRate).where(WorkRate.key == PRICE_AUTOCALC_BASE_LEVEL)) is not None
    assert db.scalar(select(WorkRate).where(WorkRate.key == PRICE_AUTOCALC_DISCOUNTS)) is not None


def test_http_apply_and_forbidden(memory_db):
    db = memory_db
    admin = _seed_user(db, username="senior", role=UserRole.ADMIN_SENIOR)
    cat = ServiceCategory(name="Услуги", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Услуга",
        is_active=True,
        price_senior_from=3000.0,
        price_senior_to=4000.0,
    )
    db.add(svc)
    db.commit()

    client = _client_for(db, admin)
    try:
        r = client.get("/admin/catalog/price-autocalc")
        assert r.status_code == 200
        assert "От какого уровня считаем" in r.text
        assert "Скидка для остальных уровней" in r.text

        r = client.post(
            "/admin/catalog/price-autocalc",
            data={
                "base_level": "SENIOR",
                "discount_MIDDLE": "20",
                "discount_JUNIOR": "40",
                "scope_mode": "all",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "msg=done" in r.headers.get("location", "")
        db.refresh(svc)
        assert svc.price_middle_from == 2400.0
        assert svc.price_junior_from == 1800.0
        base, discounts = _load_autocalc_prefs(db)
        assert base == "SENIOR"
        assert discounts["MIDDLE"] == 20.0
        assert discounts["JUNIOR"] == 40.0

        r2 = client.get(r.headers["location"])
        assert r2.status_code == 200
        assert "Готово:" in r2.text
        assert "−20%" in r2.text or "−20%" in r2.text.replace("−", "-")
    finally:
        _clear_overrides()

    master = _seed_user(db, username="master_x", role=UserRole.MASTER)
    client_m = _client_for(db, master)
    try:
        r = client_m.get("/admin/catalog/price-autocalc", follow_redirects=False)
        assert r.status_code == 403
        r = client_m.post(
            "/admin/catalog/price-autocalc",
            data={"base_level": "SENIOR", "discount_JUNIOR": "10", "scope_mode": "all"},
            follow_redirects=False,
        )
        assert r.status_code == 403
    finally:
        _clear_overrides()
