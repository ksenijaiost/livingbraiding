from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    CatalogProduct,
    Kit,
    KitAuditLog,
    KitBlanksCondition,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.db.session import get_db
from app.kit_composition_lines import BlankCondition, CompositionLine, lines_to_json
from app.main import app


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


def _seed_super(db) -> User:
    u = User(
        username="super_bulk",
        display_name="Super",
        password_hash="x",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.ADMIN_SUPER))
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


def _seed_kit_with_price(db) -> Kit:
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="SE Body",
            price=200.0,
            meta_json=json.dumps({"kit_key": "SE_BODY"}),
            is_active=True,
        )
    )
    composition = lines_to_json(
        [CompositionLine(key="SE_BODY", condition=BlankCondition.NEW, by_staff={1: 3})]
    )
    kit = Kit(
        sku="BULK-ROUTE-1",
        title="Bulk route kit",
        blank_type_de=False,
        blank_type_se=True,
        blanks_condition=KitBlanksCondition.NEW,
        pieces_total=3,
        pieces_available=3,
        stock_price_total=100.0,
        cost_total=50.0,
        discount_percent=0,
        composition_json=composition,
        is_active=True,
    )
    db.add(kit)
    db.commit()
    db.refresh(kit)
    return kit


def test_bulk_price_update_action_confirm_writes_price_and_audit(memory_db) -> None:
    db = memory_db
    kit = _seed_kit_with_price(db)
    user = _seed_super(db)
    client = _client_for(db, user)
    try:
        r = client.post(
            "/kits/bulk-price-update",
            data={"kit_ids": str(kit.id), "action": "confirm"},
        )
        assert r.status_code == 200
        assert "обновлено" in r.text or "Готово" in r.text
        db.refresh(kit)
        assert float(kit.stock_price_total) == 600.0
        audits = list(db.scalars(select(KitAuditLog).where(KitAuditLog.kit_id == kit.id)).all())
        assert any(
            (a.field_name in ("stock_price_total", "Складская цена"))
            and (a.new_value or "").startswith("600")
            for a in audits
        )
    finally:
        _clear_overrides()


def test_bulk_price_update_confirm_flag_without_action_writes(memory_db) -> None:
    db = memory_db
    kit = _seed_kit_with_price(db)
    user = _seed_super(db)
    client = _client_for(db, user)
    try:
        r = client.post(
            "/kits/bulk-price-update",
            data={"kit_ids": str(kit.id), "confirm": "1"},
        )
        assert r.status_code == 200
        db.refresh(kit)
        assert float(kit.stock_price_total) == 600.0
        audits = list(db.scalars(select(KitAuditLog).where(KitAuditLog.kit_id == kit.id)).all())
        assert audits
    finally:
        _clear_overrides()


def test_bulk_price_update_default_without_confirm_is_preview(memory_db) -> None:
    db = memory_db
    kit = _seed_kit_with_price(db)
    user = _seed_super(db)
    client = _client_for(db, user)
    try:
        r = client.post(
            "/kits/bulk-price-update",
            data={"kit_ids": str(kit.id)},
        )
        assert r.status_code == 200
        assert "Превью" in r.text
        db.refresh(kit)
        assert float(kit.stock_price_total) == 100.0
    finally:
        _clear_overrides()
