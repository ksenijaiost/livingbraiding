from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Kit, KitBlanksCondition
from app.kit_bulk_import import allocate_unique_kit_sku, import_single_kit_row, parse_bulk_kits_json


@pytest.fixture()
def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _minimal_kit_row(**overrides):
    base = {
        "sku": "SKU-IMP-1",
        "title": "Импорт тест",
        "blank_type_de": True,
        "blank_type_se": False,
        "pieces_initial": 5,
        "stock_price_total": 1000,
        "cost_total": 400,
        "discount_percent": 0,
    }
    base.update(overrides)
    return base


def test_parse_bulk_kits_json_accepts_list() -> None:
    rows = parse_bulk_kits_json(json.dumps([{"sku": "A", "x": 1}]))
    assert len(rows) == 1
    assert rows[0]["sku"] == "A"


def test_parse_bulk_kits_json_rejects_non_list() -> None:
    with pytest.raises(ValueError, match="массивом"):
        parse_bulk_kits_json('{"sku":1}')


def test_allocate_unique_first_free(memory_db: Session) -> None:
    db = memory_db
    assert allocate_unique_kit_sku(db, "NEW-SKU", set()) == "NEW-SKU"


def test_allocate_unique_suffix_when_taken(memory_db: Session) -> None:
    db = memory_db
    db.add(
        Kit(
            sku="DUP",
            title="t",
            pieces_total=1,
            pieces_available=1,
            stock_price_total=10.0,
            cost_total=5.0,
            discount_percent=0,
            created_at=datetime(2024, 1, 1, 12, 0, 0),
        )
    )
    db.commit()
    got = allocate_unique_kit_sku(db, "DUP", set())
    assert got == "DUP❗повтор"


def test_allocate_unique_reserved_in_batch(memory_db: Session) -> None:
    db = memory_db
    reserved = {"BATCH"}
    assert allocate_unique_kit_sku(db, "BATCH", reserved) == "BATCH❗повтор"


def test_import_scalar_kit(memory_db: Session) -> None:
    db = memory_db
    reserved: set[str] = set()
    r = import_single_kit_row(
        db,
        _minimal_kit_row(sku="S1"),
        reserved_skus=reserved,
        changed_by_user_id=1,
    )
    assert r["ok"] is True
    assert r["saved_sku"] == "S1"
    assert r["kit_id"] is not None
    kid = int(r["kit_id"])
    k = db.get(Kit, kid)
    assert k is not None
    assert int(k.pieces_available) == 5
    assert k.composition_json is None


def test_import_composition_without_blank_stock_fails(memory_db: Session) -> None:
    db = memory_db
    row = _minimal_kit_row(
        sku="S2",
        composition={"DE": 1},
    )
    r = import_single_kit_row(db, row, reserved_skus=set(), changed_by_user_id=1)
    assert r["ok"] is False
    msg = (r["message"] or "").lower()
    assert "blank_stock" in msg or "остаток" in msg


def test_import_two_same_sku_gets_suffix(memory_db: Session) -> None:
    db = memory_db
    reserved: set[str] = set()
    row = _minimal_kit_row(sku="SAME")
    r1 = import_single_kit_row(db, row, reserved_skus=reserved, changed_by_user_id=1)
    r2 = import_single_kit_row(db, dict(row), reserved_skus=reserved, changed_by_user_id=1)
    assert r1["ok"] and r2["ok"]
    assert r1["saved_sku"] == "SAME"
    assert r2["saved_sku"] == "SAME❗повтор"


def test_import_blanks_condition_used(memory_db: Session) -> None:
    db = memory_db
    r = import_single_kit_row(
        db,
        _minimal_kit_row(sku="S-BU", blanks_condition="USED"),
        reserved_skus=set(),
        changed_by_user_id=1,
    )
    assert r["ok"] is True
    k = db.get(Kit, int(r["kit_id"]))
    assert k is not None
    assert k.blanks_condition == KitBlanksCondition.USED
