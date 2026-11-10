from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import CatalogProduct, User, UserRole, UserRoleAssignment
from app.db.session import get_db
from app.main import app
from app.routes.products_catalog import catalog_blank_kit_key_integrity_issues
from app.seed import _ensure_zakaz_products_catalog
from app.zakaz_blanks import validate_kit_key_format


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


def _seed_user(db, *, username: str = "super", role: UserRole = UserRole.ADMIN_SUPER) -> User:
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


def _add_blank(db, *, name: str, kit_key: str, is_active: bool = True) -> CatalogProduct:
    row = CatalogProduct(
        category_name="Заказ",
        subcategory_name="Заготовки поштучно",
        name=name,
        price=100.0,
        meta_json=json.dumps({"kit_key": kit_key, "master_pay": 10.0}, ensure_ascii=False),
        sort_order=0,
        is_active=is_active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_validate_kit_key_format_ok_and_bad() -> None:
    assert validate_kit_key_format("de_curl_heart") == "DE_CURL_HEART"
    assert validate_kit_key_format("  SE_BRAID_LONG ") == "SE_BRAID_LONG"
    for bad in ("SE_", "DE_", "DE__X", "SE BRAID", "СЕ_КОСА", "FOO_BAR", ""):
        with pytest.raises(ValueError):
            validate_kit_key_format(bad)


def _count_blanks(db) -> int:
    return len(
        list(
            db.scalars(
                select(CatalogProduct).where(
                    CatalogProduct.category_name == "Заказ",
                    CatalogProduct.subcategory_name == "Заготовки поштучно",
                )
            ).all()
        )
    )


def test_create_duplicate_kit_key_redirect(memory_db) -> None:
    db = memory_db
    _add_blank(db, name="D.E. термокудря", kit_key="DE_CURL")
    user = _seed_user(db)
    client = _client_for(db, user)
    try:
        r = client.post(
            "/products-catalog/new",
            data={
                "category": "Заказ",
                "subcategory_name": "Заготовки поштучно",
                "name": "D.E. термокудря (сердечко)",
                "kit_key": "de_curl",
                "price": "200",
                "is_active": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "duplicate_kit_key" in (r.headers.get("location") or "")
        assert _count_blanks(db) == 1
    finally:
        _clear_overrides()


def test_edit_to_foreign_kit_key_rejected(memory_db) -> None:
    db = memory_db
    a = _add_blank(db, name="A", kit_key="DE_CURL")
    b = _add_blank(db, name="B", kit_key="DE_CURL_HEART")
    user = _seed_user(db)
    client = _client_for(db, user)
    try:
        r = client.post(
            f"/products-catalog/{b.id}/edit",
            data={
                "category": "Заказ",
                "name": "B",
                "kit_key": "DE_CURL",
                "price": "100",
                "is_active": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "duplicate_kit_key" in (r.headers.get("location") or "")
        db.refresh(b)
        assert json.loads(b.meta_json or "{}").get("kit_key") == "DE_CURL_HEART"
        assert a.id != b.id
    finally:
        _clear_overrides()


def test_create_bad_and_ok_kit_key(memory_db) -> None:
    db = memory_db
    user = _seed_user(db)
    client = _client_for(db, user)
    try:
        for bad in ("SE_", "DE_"):
            r = client.post(
                "/products-catalog/new",
                data={
                    "category": "Заказ",
                    "subcategory_name": "Заготовки поштучно",
                    "name": f"Bad {bad}",
                    "kit_key": bad,
                    "price": "10",
                    "is_active": "1",
                },
                follow_redirects=False,
            )
            assert r.status_code == 303
            assert "bad_kit_key_format" in (r.headers.get("location") or "")
        r_ok = client.post(
            "/products-catalog/new",
            data={
                "category": "Заказ",
                "subcategory_name": "Заготовки поштучно",
                "name": "D.E. термокудря сердце",
                "kit_key": "DE_CURL_HEART",
                "price": "200",
                "is_active": "1",
            },
            follow_redirects=False,
        )
        assert r_ok.status_code == 303
        assert "msg=saved" in (r_ok.headers.get("location") or "")
        row = db.scalar(
            select(CatalogProduct).where(CatalogProduct.name == "D.E. термокудря сердце")
        )
        assert row is not None
        assert json.loads(row.meta_json or "{}").get("kit_key") == "DE_CURL_HEART"
    finally:
        _clear_overrides()


def test_edit_renames_kit_key(memory_db) -> None:
    db = memory_db
    row = _add_blank(db, name="Микрокоса", kit_key="SE_")
    # Precondition: invalid key in DB; edit to valid unique key.
    user = _seed_user(db)
    client = _client_for(db, user)
    try:
        r = client.post(
            f"/products-catalog/{row.id}/edit",
            data={
                "category": "Заказ",
                "name": "S.E. микрокоса",
                "kit_key": "SE_MICRO_BRAID",
                "price": "100",
                "is_active": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "msg=saved" in (r.headers.get("location") or "")
        db.refresh(row)
        assert json.loads(row.meta_json or "{}").get("kit_key") == "SE_MICRO_BRAID"
        assert row.name == "S.E. микрокоса"
    finally:
        _clear_overrides()


def test_seed_does_not_duplicate_kit_key(memory_db, caplog) -> None:
    db = memory_db
    _add_blank(db, name="Чужая термокудря", kit_key="DE_CURL")
    with caplog.at_level(logging.WARNING, logger="app.seed"):
        _ensure_zakaz_products_catalog(db)
        db.commit()
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
            )
        ).all()
    )
    curl_owners = []
    for r in rows:
        meta = json.loads(r.meta_json or "{}")
        if str(meta.get("kit_key") or "").strip().upper() == "DE_CURL":
            curl_owners.append(r)
    assert len(curl_owners) == 1
    assert curl_owners[0].name == "Чужая термокудря"
    assert any("kit_key=DE_CURL" in rec.message for rec in caplog.records)


def test_integrity_warning_on_catalog_page(memory_db) -> None:
    db = memory_db
    _add_blank(db, name="A", kit_key="DE_CURL")
    _add_blank(db, name="B", kit_key="de_curl", is_active=False)
    _add_blank(db, name="C", kit_key="SE_")
    issues = catalog_blank_kit_key_integrity_issues(db)
    assert issues["has_issues"] is True
    assert any(d["kit_key"] == "DE_CURL" and len(d["rows"]) == 2 for d in issues["duplicates"])
    assert any(r["kit_key"] == "SE_" for r in issues["bad_format"])

    user = _seed_user(db)
    client = _client_for(db, user)
    try:
        page = client.get("/products-catalog?category=%D0%97%D0%B0%D0%BA%D0%B0%D0%B7")
        assert page.status_code == 200
        body = page.text
        assert "проблемы с" in body
        assert "DE_CURL" in body
        assert "SE_" in body
    finally:
        _clear_overrides()
