from __future__ import annotations

from app.db.models import WorkScope
from app.work_products_compute import (
    kit_studio_profit_amount,
    resolve_kit_client_price,
)


def test_resolve_kit_client_price_custom_order_override() -> None:
    assert resolve_kit_client_price(
        scope=WorkScope.CUSTOM_ORDER,
        catalog_client_price=5000.0,
        amount_from_client=8750.0,
    ) == 8750.0
    assert resolve_kit_client_price(
        scope=WorkScope.CUSTOM_ORDER,
        catalog_client_price=5000.0,
        amount_from_client=None,
    ) == 5000.0
    assert resolve_kit_client_price(
        scope=WorkScope.IN_STOCK,
        catalog_client_price=5000.0,
        amount_from_client=8750.0,
    ) == 5000.0


def test_kit_studio_profit_amount_formula() -> None:
    # цена 8750, себестоимость 1600, мастер 1450 (+ смешка) -> студия 5700
    studio = kit_studio_profit_amount(
        scope=WorkScope.CUSTOM_ORDER,
        client_price=8750.0,
        cost_total=1600.0,
        master_total=1450.0,
    )
    assert studio == 5700.0

    assert kit_studio_profit_amount(
        scope=WorkScope.IN_STOCK,
        client_price=8750.0,
        cost_total=1600.0,
        master_total=1450.0,
    ) == 0.0
