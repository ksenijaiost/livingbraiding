from __future__ import annotations

import json

import pytest
from starlette.datastructures import FormData

from app.kit_composition import KIT_INVENTORY_PIECE_EXCLUDE_KEYS, composition_json_from_totals
from app.db.models import KitBlanksCondition
from app.kit_composition_lines import BlankCondition, CompositionLine
from app.kit_crud import (
    apply_kit_admin_form,
    estimate_kit_admin_cost_total,
    parse_kit_admin_form,
    parse_kit_qty_totals_from_form,
    try_fill_kit_admin_cost_total_from_composition,
    try_fill_kit_admin_stock_price_total_from_composition,
    validate_kit_admin_form,
)
from app.db.models import MaterialPriceCurrent, MaterialType


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
            ("blanks_condition", "MIXED"),
            ("kit_qty_0_SE_TRIM_SHORT", "2"),
            ("kit_qty_0_BODY", "4"),
        ]
    )
    d = parse_kit_admin_form(form, for_create=True)
    assert d.composition_totals == {"SE_TRIM_SHORT": 2, "BODY": 4}
    assert d.pieces_total == 4
    assert d.pieces_available == 4
    assert d.blanks_condition == KitBlanksCondition.MIXED
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
            ("blanks_condition", "USED"),
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
    assert kit.blanks_condition == KitBlanksCondition.USED
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


def test_estimate_kit_admin_cost_total_work_and_weight(memory_db) -> None:
    from app.db.models import CatalogProduct

    memory_db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="Тело",
            price=500.0,
            is_active=True,
            meta_json='{"kit_key": "SE_BODY", "master_pay": 100}',
        )
    )
    memory_db.add(
        MaterialPriceCurrent(material_type=MaterialType.KANEKALON, price_per_gram=2.5)
    )
    memory_db.commit()
    lines = [
        CompositionLine(
            key="SE_BODY",
            condition=BlankCondition.NEW,
            used_price_pct=100,
            by_staff={0: 2},
        ),
        CompositionLine(
            key="SE_BODY",
            condition=BlankCondition.USED,
            used_price_pct=50,
            by_staff={0: 1},
        ),
    ]
    expected = 100.0 * 2 + 100.0 * 0.5 * 1 + 100.0 * 2.5
    got = estimate_kit_admin_cost_total(memory_db, lines, weight_grams=100.0)
    assert got == pytest.approx(expected)


def test_try_fill_stock_and_cost_from_composition_lines(memory_db) -> None:
    from app.db.models import CatalogProduct

    memory_db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="Тело",
            price=500.0,
            is_active=True,
            meta_json='{"kit_key": "SE_BODY", "master_pay": 80}',
        )
    )
    memory_db.add(
        MaterialPriceCurrent(material_type=MaterialType.KANEKALON, price_per_gram=1.0)
    )
    memory_db.commit()

    form = FormData(
        [
            ("sku", "AUTO"),
            ("title", "Auto"),
            ("blank_type_se", "on"),
            ("discount_percent", "0"),
            ("weight_grams", "10"),
            ("kit_line_0_key", "SE_BODY"),
            ("kit_line_0_qty_0", "2"),
        ]
    )
    d = parse_kit_admin_form(form, for_create=True)
    assert d.stock_price_total is None
    assert d.cost_total is None
    try_fill_kit_admin_stock_price_total_from_composition(
        memory_db, d, composition_totals=d.composition_totals
    )
    try_fill_kit_admin_cost_total_from_composition(memory_db, d)
    assert d.stock_price_total == pytest.approx(1000.0)
    assert d.cost_total == pytest.approx(160.0 + 10.0)
