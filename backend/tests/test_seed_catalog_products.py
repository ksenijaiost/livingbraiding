from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import CatalogProduct
from app.seed import _upsert_catalog_product, ensure_prod_seed_data


@pytest.fixture()
def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_upsert_catalog_product_preserves_meta_when_seed_meta_empty(memory_db: Session) -> None:
    db = memory_db
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Коррекция комплекта",
            name="Стирка",
            price=400.0,
            meta_json=json.dumps({"master_pay": 100.0, "fixed_expense": 50.0}, ensure_ascii=False),
            sort_order=0,
            is_active=True,
        )
    )
    db.commit()

    _upsert_catalog_product(
        db,
        category_name="Заказ",
        subcategory_name="Коррекция комплекта",
        name="Стирка",
        price=400.0,
        meta={},
        sort_order=0,
    )
    db.commit()

    row = db.scalar(
        select(CatalogProduct).where(
            CatalogProduct.category_name == "Заказ",
            CatalogProduct.subcategory_name == "Коррекция комплекта",
            CatalogProduct.name == "Стирка",
        )
    )
    assert row is not None
    meta = json.loads(row.meta_json or "{}")
    assert meta.get("master_pay") == 100.0
    assert meta.get("fixed_expense") == 50.0


def test_prod_seed_does_not_wipe_catalog_products_meta(memory_db: Session) -> None:
    db = memory_db
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            name="Хвост 60",
            price=1500.0,
            meta_json=json.dumps({"master_pay": 200.0, "fixed_expense": 100.0}, ensure_ascii=False),
            sort_order=0,
            is_active=True,
        )
    )
    db.commit()

    ensure_prod_seed_data(db)

    row = db.scalar(
        select(CatalogProduct).where(
            CatalogProduct.category_name == "Заказ",
            CatalogProduct.subcategory_name == "Хвосты/резинки",
            CatalogProduct.name == "Хвост 60",
        )
    )
    assert row is not None
    meta = json.loads(row.meta_json or "{}")
    assert meta.get("master_pay") == 200.0
    assert meta.get("fixed_expense") == 100.0
