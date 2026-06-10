from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import CatalogProduct
from app.zakaz_blanks import kit_composition_catalog_items, section_from_kit_key


def test_section_from_kit_key_prefix() -> None:
    assert section_from_kit_key("DE_DREAD_LONG") == "DE"
    assert section_from_kit_key("se_braid_short") == "SE"
    assert section_from_kit_key("FOO_BAR") is None


def test_kit_composition_catalog_merges_db_rows() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    db = Sess()
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="Новая SE коса",
            price=99.0,
            meta_json=json.dumps({"kit_key": "SE_CUSTOM_NEW"}),
            sort_order=1,
            is_active=True,
        )
    )
    db.commit()

    items = kit_composition_catalog_items(db)
    by_key = {x["key"]: x for x in items}
    assert "SE_CUSTOM_NEW" in by_key
    assert by_key["SE_CUSTOM_NEW"]["section"] == "SE"
    assert by_key["SE_CUSTOM_NEW"]["label"] == "Новая SE коса"
    assert "DE_DREAD_LONG" in by_key
