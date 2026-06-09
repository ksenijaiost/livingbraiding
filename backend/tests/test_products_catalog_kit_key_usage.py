from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Kit, KitBlankStock
from app.routes.products_catalog import catalog_blank_kit_key_usage


def test_catalog_blank_kit_key_usage_counts_active_and_archived() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    db = Sess()

    db.add(
        Kit(
            sku="K-ACTIVE",
            title="Активный",
            is_active=True,
            is_archived=False,
            composition_json=json.dumps([{"key": "SE_BRAID_LONG", "qty": 2}]),
        )
    )
    db.add(
        Kit(
            sku="K-ARCH",
            title="Архив",
            is_active=True,
            is_archived=True,
            composition_json=json.dumps([{"key": "SE_BRAID_LONG", "qty": 1}]),
        )
    )
    other = Kit(sku="K-OTHER", title="Other", is_active=False, is_archived=False, composition_json=None)
    db.add(other)
    db.flush()
    db.add(KitBlankStock(kit_id=int(other.id), kit_key="SE_BRAID_LONG", qty=1))
    db.commit()

    usage = catalog_blank_kit_key_usage(db, "SE_BRAID_LONG")
    assert usage["total"] == 3
    assert usage["active"] == 1
    assert usage["kits"][0]["sku"] == "K-ACTIVE"
