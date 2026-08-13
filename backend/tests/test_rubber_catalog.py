from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import CatalogProduct
from app.rubber_catalog import (
    build_rubber_catalog_display,
    enable_rubber_sizes,
    find_rubber_catalog_product,
    normalize_rubber_catalog_names,
    rubber_family_size_from_type,
    rubber_pricing_tuple,
    rubber_service_name,
    rubber_type_from_catalog_name,
    split_rubber_catalog_name,
)
from app.work_products import _rubber_pricing_from_catalog


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_split_rubber_catalog_name_aliases() -> None:
    assert split_rubber_catalog_name("Хвост на резинке (1 крепление) mini") == (
        "Хвост на резинке (1 крепление)",
        "MINI",
    )
    assert split_rubber_catalog_name("Хвост на резинке (1 крепление) standar") == (
        "Хвост на резинке (1 крепление)",
        "STANDARD",
    )
    assert split_rubber_catalog_name("Хвост на крабе — mini") == ("Хвост на крабе", "MINI")
    assert split_rubber_catalog_name("Косы на резинке (1 коса)") == ("Косы на резинке (1 коса)", None)


def test_rubber_type_from_messy_name() -> None:
    assert rubber_type_from_catalog_name("Хвост на резинке (1 крепление) max") == "TAIL_ELASTIC_MAX"
    assert rubber_service_name("TAIL_ELASTIC_STANDARD") == "Хвост на резинке (1 крепление) — standard"
    assert rubber_family_size_from_type("TAIL_ELASTIC") == ("TAIL_ELASTIC", "")


def test_find_rubber_catalog_with_typo_and_legacy_fallback() -> None:
    db = _db()
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            name="Хвост на резинке (1 крепление) standar",
            price=250.0,
            meta_json=json.dumps({"master_pay": 30.0, "fixed_expense": 50.0, "is_per_unit": True}),
            is_active=True,
        )
    )
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            name="Хвост на резинке (1 крепление) mini",
            price=150.0,
            meta_json=json.dumps({"master_pay": 25.0, "fixed_expense": 40.0, "is_per_unit": True}),
            is_active=True,
        )
    )
    db.commit()

    std = find_rubber_catalog_product(db, "TAIL_ELASTIC")
    assert std is not None
    assert "standar" in std.name or "standard" in std.name.lower()
    mp, _sp, fx, per_unit, _ul = _rubber_pricing_from_catalog(db, "TAIL_ELASTIC")
    assert mp == 30.0
    assert fx == 50.0
    assert per_unit is True

    mini = find_rubber_catalog_product(db, "TAIL_ELASTIC_MINI")
    assert mini is not None
    assert rubber_pricing_tuple(mini, "TAIL_ELASTIC")[0] == 25.0


def test_normalize_rubber_catalog_names() -> None:
    db = _db()
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            name="Хвост на резинке (1 крепление) standar",
            price=250.0,
            meta_json="{}",
            is_active=True,
        )
    )
    db.commit()
    assert normalize_rubber_catalog_names(db) >= 1
    db.commit()
    row = db.scalar(select(CatalogProduct))
    assert row is not None
    assert row.name == "Хвост на резинке (1 крепление) — standard"


def test_enable_rubber_sizes_splits_unsized() -> None:
    db = _db()
    row = CatalogProduct(
        category_name="Заказ",
        subcategory_name="Хвосты/резинки",
        name="Косы на резинке (1 коса)",
        price=85.0,
        meta_json=json.dumps({"master_pay": 15.0, "is_per_unit": True, "unit_label": "коса"}),
        is_active=True,
    )
    db.add(row)
    db.commit()
    enable_rubber_sizes(db, row)
    db.commit()
    names = {r.name for r in db.scalars(select(CatalogProduct)).all()}
    assert "Косы на резинке (1 коса) — mini" in names
    assert "Косы на резинке (1 коса) — standard" in names
    assert "Косы на резинке (1 коса) — max" in names
    std = db.scalar(
        select(CatalogProduct).where(CatalogProduct.name == "Косы на резинке (1 коса) — standard")
    )
    assert std is not None
    assert std.price == 85.0


def test_build_rubber_catalog_display_groups() -> None:
    rows = [
        SimpleNamespace(
            id=1,
            name="Хвост на резинке (1 крепление) — mini",
            price=150.0,
            master_pay=25.0,
            fixed_expense=40.0,
            is_active=True,
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            kit_key=None,
            ignore_in_calc=False,
            is_used_in_kit_form=False,
            is_bu=False,
        ),
        SimpleNamespace(
            id=2,
            name="Хвост на резинке (1 крепление) — standard",
            price=250.0,
            master_pay=30.0,
            fixed_expense=50.0,
            is_active=True,
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            kit_key=None,
            ignore_in_calc=False,
            is_used_in_kit_form=False,
            is_bu=False,
        ),
        SimpleNamespace(
            id=3,
            name="Косы на резинке (1 коса)",
            price=85.0,
            master_pay=15.0,
            fixed_expense=0.0,
            is_active=True,
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            kit_key=None,
            ignore_in_calc=False,
            is_used_in_kit_form=False,
            is_bu=False,
        ),
    ]
    display = build_rubber_catalog_display(rows)
    kinds = [d.row_kind for d in display]
    assert kinds[0] == "group_parent"
    assert "size_child" in kinds
    assert "size_placeholder" in kinds
    assert display[-1].row_kind == "plain"
    assert display[-1].size_checked is False
