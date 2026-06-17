from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    CatalogProduct,
    Client,
    ProductSale,
    ProductSaleKind,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    WorkForInventory,
    WorkKind,
    WorkScope,
)
from app.routes.products_catalog import catalog_product_delete_usage
from app.work_products import _rubber_service_name
from app.work_products_compute import CORR_SVC_TRIM


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Sess()


def _seed_user(db) -> User:
    user = User(username="u1", password_hash="x", display_name="U", role=UserRole.ADMIN, is_active=True)
    db.add(user)
    db.flush()
    return user


def test_delete_preview_other_works() -> None:
    db = _db()
    user = _seed_user(db)
    row = CatalogProduct(
        category_name="Заказ",
        subcategory_name="Другое",
        name="Тестовый товар",
        is_active=False,
        meta_json="{}",
    )
    db.add(row)
    db.flush()
    db.add(
        WorkForInventory(
            kind=WorkKind.OTHER,
            scope=WorkScope.IN_STOCK,
            details_json=json.dumps({"other": {"catalog_product_id": int(row.id)}}),
            performed_date=datetime.utcnow(),
            created_by_user_id=int(user.id),
        )
    )
    db.commit()

    usage = catalog_product_delete_usage(db, row)
    other = next(c for c in usage["checks"] if c["kind"] == "other_works")
    assert other["total"] == 1
    assert usage["has_usage"] is True


def test_delete_preview_rubber_works() -> None:
    db = _db()
    user = _seed_user(db)
    svc_name = _rubber_service_name("TAIL_CRAB_MINI")
    row = CatalogProduct(
        category_name="Заказ",
        subcategory_name="Хвосты/резинки",
        name=svc_name,
        is_active=False,
        meta_json="{}",
    )
    db.add(row)
    db.flush()
    db.add(
        WorkForInventory(
            kind=WorkKind.RUBBER,
            scope=WorkScope.IN_STOCK,
            details_json=json.dumps({"rubber": {"type": "TAIL_CRAB_MINI", "qty": 1}}),
            performed_date=datetime.utcnow(),
            created_by_user_id=int(user.id),
        )
    )
    db.commit()

    usage = catalog_product_delete_usage(db, row)
    rubber = next(c for c in usage["checks"] if c["kind"] == "rubber_works")
    assert rubber["total"] == 1
    assert usage["has_usage"] is True


def test_delete_preview_correction_works() -> None:
    db = _db()
    user = _seed_user(db)
    row = CatalogProduct(
        category_name="Заказ",
        subcategory_name="Коррекция комплекта",
        name=CORR_SVC_TRIM,
        is_active=False,
        meta_json="{}",
    )
    db.add(row)
    db.flush()
    db.add(
        WorkForInventory(
            kind=WorkKind.KIT_CORRECTION,
            scope=WorkScope.IN_STOCK,
            details_json=json.dumps({"correction": {"trim_qty": 2, "hourly_hours": 0.0, "wash": False}}),
            performed_date=datetime.utcnow(),
            created_by_user_id=int(user.id),
        )
    )
    db.commit()

    usage = catalog_product_delete_usage(db, row)
    corr = next(c for c in usage["checks"] if c["kind"] == "correction_works")
    assert corr["total"] == 1


def test_delete_preview_material_sales() -> None:
    db = _db()
    cat = ServiceCategory(name="Продажа материала", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=int(cat.id), name="Канекалон", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=int(sub.id), name="Чёрный 60 см", is_active=True)
    db.add(svc)
    db.flush()
    row = CatalogProduct(
        category_name="Продажа материала",
        subcategory_name="Канекалон",
        name="Чёрный 60 см",
        is_active=False,
        meta_json="{}",
    )
    db.add(row)
    user = _seed_user(db)
    client = Client(name="C", phone="+70000000000")
    db.add(client)
    db.flush()
    db.add(
        ProductSale(
            kind=ProductSaleKind.MATERIAL,
            material_service_id=int(svc.id),
            client_id=int(client.id),
            created_by_user_id=int(user.id),
            performed_date=datetime.utcnow(),
            amount_from_client=100,
            is_voided=False,
        )
    )
    db.commit()

    usage = catalog_product_delete_usage(db, row)
    sales = next(c for c in usage["checks"] if c["kind"] == "material_sales")
    assert sales["total"] == 1
    assert usage["has_usage"] is True
