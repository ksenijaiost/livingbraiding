"""Маржа продажи товаров: % оформившего в личный фонд; остаток − себестоимость → студия."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import ProductSaleKind
from app.payroll_fund import (
    compute_product_sale_studio_margin,
    product_sale_goods_cost,
    product_sale_seller_commission,
)


def test_seller_commission_10() -> None:
    sale = SimpleNamespace(amount_from_client=1000, sale_percent=10)
    assert product_sale_seller_commission(sale) == 100.0  # type: ignore[arg-type]


def test_seller_commission_15() -> None:
    sale = SimpleNamespace(amount_from_client=2000, sale_percent=15)
    assert product_sale_seller_commission(sale) == 300.0  # type: ignore[arg-type]


def test_seller_commission_absent_legacy() -> None:
    sale = SimpleNamespace(amount_from_client=1000, sale_percent=None)
    assert product_sale_seller_commission(sale) == 0.0  # type: ignore[arg-type]


def test_studio_after_percent_and_rubber_cost() -> None:
    # 1000 − 10% − 300 = 600 в студию
    sale = SimpleNamespace(
        kind=ProductSaleKind.RUBBER,
        amount_from_client=1000,
        sale_percent=10,
        rubber_price_override=300,
        material_cost_review_pending=False,
    )
    assert compute_product_sale_studio_margin(None, sale) == 600.0  # type: ignore[arg-type]


def test_studio_after_percent_other_cost_zero() -> None:
    # 500 − 15% − 0 = 425
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=500,
        sale_percent=15,
        other_cost=None,
        material_cost_review_pending=False,
    )
    assert compute_product_sale_studio_margin(None, sale) == 425.0  # type: ignore[arg-type]


def test_studio_not_negative_when_cost_high() -> None:
    # 100 − 10% − 250 = 0
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=100,
        sale_percent=10,
        other_cost=250,
        material_cost_review_pending=False,
    )
    assert compute_product_sale_studio_margin(None, sale) == 0.0  # type: ignore[arg-type]


def test_rubber_margin_subtracts_cost_legacy_without_percent() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.RUBBER,
        amount_from_client=1000,
        sale_percent=None,
        rubber_price_override=300,
    )
    assert compute_product_sale_studio_margin(None, sale) == 700.0  # type: ignore[arg-type]


def test_rubber_margin_zero_when_cost_missing_legacy() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.RUBBER,
        amount_from_client=500,
        sale_percent=None,
        rubber_price_override=None,
    )
    assert compute_product_sale_studio_margin(None, sale) == 500.0  # type: ignore[arg-type]


def test_other_margin_subtracts_cost() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=500,
        sale_percent=None,
        other_cost=120.5,
    )
    assert compute_product_sale_studio_margin(None, sale) == 379.5  # type: ignore[arg-type]


def test_margin_not_negative() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=100,
        sale_percent=None,
        other_cost=250,
    )
    assert compute_product_sale_studio_margin(None, sale) == 0.0  # type: ignore[arg-type]


def test_other_without_cost_and_percent_uses_full_amount_legacy() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=400,
        sale_percent=None,
        other_cost=None,
    )
    assert compute_product_sale_studio_margin(None, sale) == 400.0  # type: ignore[arg-type]


def test_goods_cost_rubber_and_other() -> None:
    db = MagicMock()
    rubber = SimpleNamespace(kind=ProductSaleKind.RUBBER, rubber_price_override=120)
    other = SimpleNamespace(kind=ProductSaleKind.OTHER, other_cost=None)
    assert product_sale_goods_cost(db, rubber) == 120.0  # type: ignore[arg-type]
    assert product_sale_goods_cost(db, other) == 0.0  # type: ignore[arg-type]
