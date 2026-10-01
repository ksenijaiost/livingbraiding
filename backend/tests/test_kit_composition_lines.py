"""Состав комплекта v2: NEW/USED по строкам."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    CatalogProduct,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.kit_composition_lines import (
    BlankCondition,
    CompositionLine,
    client_price_for_lines,
    filter_nonempty,
    inventory_totals_by_key,
    lines_from_form,
    lines_to_json,
    lines_to_legacy_totals,
    work_pay_for_lines,
)
from app.kit_inlay_visit import get_salon_cut_pct


class _FakeForm(dict):
    def keys(self):
        return super().keys()

    def get(self, key, default=None):
        return super().get(key, default)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_catalog(db, *, price: float = 150.0, master_pay: float = 25.0) -> None:
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="SE Braid",
            price=price,
            meta_json='{"kit_key": "SE_BRAID_LONG", "master_pay": '
            + str(master_pay)
            + "}",
            is_active=True,
        )
    )
    db.commit()


def test_lines_from_form_two_rows_same_key():
    form = _FakeForm(
        {
            "kit_line_0_key": "SE_BRAID_LONG",
            "kit_line_0_is_used": "",
            "kit_line_0_qty_1": "2",
            "kit_line_1_key": "SE_BRAID_LONG",
            "kit_line_1_is_used": "on",
            "kit_line_1_used_pct": "50",
            "kit_line_1_qty_1": "1",
            "kit_line_2_key": "",
        }
    )
    lines = lines_from_form(form)
    assert len(lines) == 2
    assert lines[0].condition == BlankCondition.NEW
    assert lines[1].condition == BlankCondition.USED
    assert lines[1].used_price_pct == 50


def test_client_price_new_and_used(memory_db):
    _seed_catalog(memory_db, price=150.0)
    lines = [
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={1: 2}),
        CompositionLine(
            key="SE_BRAID_LONG",
            condition=BlankCondition.USED,
            used_price_pct=50,
            by_staff={1: 1},
        ),
    ]
    total, missing = client_price_for_lines(memory_db, lines)
    assert missing == []
    assert total == pytest.approx(150.0 * 2 + 150.0 * 0.5 * 1)


def test_work_pay_only_new(memory_db):
    _seed_catalog(memory_db, master_pay=25.0)
    lines = [
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={1: 2}),
        CompositionLine(
            key="SE_BRAID_LONG",
            condition=BlankCondition.USED,
            used_price_pct=50,
            by_staff={1: 3},
        ),
    ]
    pay = work_pay_for_lines(memory_db, lines)
    assert pay[1] == pytest.approx(50.0)


def test_work_pay_second_master_when_first_zero(memory_db):
    _seed_catalog(memory_db, master_pay=50.0)
    lines = [
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={1: 20}),
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={2: 10}),
    ]
    pay = work_pay_for_lines(memory_db, lines)
    assert pay[1] == pytest.approx(1000.0)
    assert pay[2] == pytest.approx(500.0)


def test_lines_from_form_second_master_only_qty(memory_db):
    _seed_catalog(memory_db, master_pay=50.0)
    form = _FakeForm(
        {
            "kit_line_0_key": "SE_BRAID_LONG",
            "kit_line_0_qty_1": "0",
            "kit_line_0_qty_2": "10",
            "kit_line_1_key": "",
        }
    )
    lines = lines_from_form(form)
    assert len(lines) == 1
    assert lines[0].by_staff == {2: 10}
    pay = work_pay_for_lines(memory_db, lines)
    assert 1 not in pay
    assert pay[2] == pytest.approx(500.0)


def test_inventory_totals_sums_new_and_used():
    lines = [
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={1: 2}),
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.USED, by_staff={1: 1}),
    ]
    assert inventory_totals_by_key(lines)["SE_BRAID_LONG"] == 3


def test_bu_correction_limit_formula(memory_db):
    _seed_catalog(memory_db, price=100.0)
    lines = [
        CompositionLine(
            key="SE_BRAID_LONG",
            condition=BlankCondition.USED,
            used_price_pct=100,
            by_staff={1: 40},
        ),
    ]
    from app.kit_composition_lines import used_client_total_for_lines

    used_total = used_client_total_for_lines(memory_db, lines)
    salon_pct = float(get_salon_cut_pct(memory_db))
    max_corr_pay = used_total * (1.0 - salon_pct)
    assert used_total == pytest.approx(4000.0)
    assert max_corr_pay == pytest.approx(2000.0)
    assert 2500.0 > max_corr_pay


def test_lines_to_json_roundtrip():
    lines = [
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={5: 1}),
    ]
    raw = lines_to_json(lines)
    assert raw is not None
    assert "by_staff" in raw
    assert "NEW" in raw


def test_unit_price_for_stock_key_separates_new_and_used(memory_db) -> None:
    from app.kit_composition_lines import (
        keyed_client_price_selected_v2,
        unit_client_price_for_stock_key,
    )
    from app.kit_blank_stock_core import load_catalog_kit_maps

    _seed_catalog(memory_db, price=100.0)
    lines = [
        CompositionLine(key="SE_BRAID_LONG", condition=BlankCondition.NEW, by_staff={1: 10}),
        CompositionLine(
            key="SE_BRAID_LONG",
            condition=BlankCondition.USED,
            used_price_pct=60,
            by_staff={1: 20},
        ),
    ]
    price_map, meta, _ = load_catalog_kit_maps(memory_db)
    assert unit_client_price_for_stock_key(
        memory_db, lines, "SE_BRAID_LONG", price_map=price_map, meta_by_key=meta
    ) == pytest.approx(100.0)
    assert unit_client_price_for_stock_key(
        memory_db, lines, "SE_BRAID_LONG__USED__", price_map=price_map, meta_by_key=meta
    ) == pytest.approx(60.0)
    raw = lines_to_json(lines)
    assert keyed_client_price_selected_v2(
        memory_db,
        raw,
        {"SE_BRAID_LONG": 2, "SE_BRAID_LONG__USED__": 5},
        price_map=price_map,
        meta_by_key=meta,
    ) == pytest.approx(2 * 100.0 + 5 * 60.0)
