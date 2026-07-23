"""Маржа продажи товаров: хвост/резинка и «Другое» = сумма с клиента − себестоимость."""

from __future__ import annotations

from types import SimpleNamespace

from app.db.models import ProductSaleKind
from app.payroll_fund import compute_product_sale_studio_margin


def test_rubber_margin_subtracts_cost() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.RUBBER,
        amount_from_client=1000,
        rubber_price_override=300,
    )
    assert compute_product_sale_studio_margin(None, sale) == 700.0  # type: ignore[arg-type]


def test_rubber_margin_zero_when_cost_missing_legacy() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.RUBBER,
        amount_from_client=500,
        rubber_price_override=None,
    )
    assert compute_product_sale_studio_margin(None, sale) == 500.0  # type: ignore[arg-type]


def test_other_margin_subtracts_cost() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=500,
        other_cost=120.5,
    )
    assert compute_product_sale_studio_margin(None, sale) == 379.5  # type: ignore[arg-type]


def test_margin_not_negative() -> None:
    sale = SimpleNamespace(
        kind=ProductSaleKind.OTHER,
        amount_from_client=100,
        other_cost=250,
    )
    assert compute_product_sale_studio_margin(None, sale) == 0.0  # type: ignore[arg-type]
