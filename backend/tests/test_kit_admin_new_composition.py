from __future__ import annotations

import json

import pytest
from starlette.datastructures import FormData

from app.kit_composition import KIT_INVENTORY_PIECE_EXCLUDE_KEYS, composition_json_from_totals
from app.kit_crud import (
    apply_kit_admin_form,
    parse_kit_admin_form,
    parse_kit_qty_totals_from_form,
    validate_kit_admin_form,
)


def test_parse_kit_qty_totals_from_form_sums_columns() -> None:
    form = FormData(
        [
            ("kit_qty_0_A", "2"),
            ("kit_qty_1_A", "3"),
            ("kit_qty_0_B", "1"),
            ("other", "x"),
        ]
    )
    got = parse_kit_qty_totals_from_form(form)
    assert got == {"A": 5, "B": 1}


def test_parse_kit_admin_form_create_uses_inventory_count_excluding_trims() -> None:
    form = FormData(
        [
            ("sku", "T-SKU"),
            ("title", "T"),
            ("blank_type_se", "on"),
            ("stock_price_total", "100"),
            ("cost_total", "40"),
            ("discount_percent", "0"),
            ("kit_qty_0_SE_TRIM_SHORT", "2"),
            ("kit_qty_0_BODY", "4"),
        ]
    )
    d = parse_kit_admin_form(form, for_create=True)
    assert d.composition_totals == {"SE_TRIM_SHORT": 2, "BODY": 4}
    assert d.pieces_total == 4
    assert d.pieces_available == 4
    validate_kit_admin_form(d, for_create=True)


def test_composition_json_from_totals_matches_list_format() -> None:
    raw = composition_json_from_totals({"Z": 1, "Y": 2})
    assert raw is not None
    payload = json.loads(raw)
    assert isinstance(payload, list)
    by_key = {x["key"]: x["qty"] for x in payload}
    assert by_key == {"Z": 1, "Y": 2}


def test_apply_kit_and_composition_roundtrip(memory_db) -> None:
    from datetime import datetime

    from app.db.models import Kit

    form = FormData(
        [
            ("sku", "R-SKU"),
            ("title", "R"),
            ("blank_type_se", "on"),
            ("stock_price_total", "200"),
            ("cost_total", "80"),
            ("discount_percent", "0"),
            ("kit_qty_0_ITEMX", "3"),
        ]
    )
    d = parse_kit_admin_form(form, for_create=True)
    validate_kit_admin_form(d, for_create=True)
    kit = Kit(created_at=datetime(2024, 1, 1, 12, 0, 0))
    apply_kit_admin_form(kit, d)
    kit.composition_json = composition_json_from_totals(d.composition_totals)
    memory_db.add(kit)
    memory_db.commit()
    memory_db.refresh(kit)
    assert kit.pieces_total == 3
    assert kit.pieces_available == 3
    comp = json.loads(kit.composition_json or "[]")
    assert comp == [{"key": "ITEMX", "qty": 3}]


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_inventory_exclude_keys_frozen() -> None:
    assert "SE_TRIM_SHORT" in KIT_INVENTORY_PIECE_EXCLUDE_KEYS
