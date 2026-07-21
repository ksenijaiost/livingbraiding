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


def test_kit_studio_profit_amount_with_client_amount() -> None:
    # сумма 8750, себестоимость 1600, мастер 1450 -> студия 5700
    studio = kit_studio_profit_amount(
        scope=WorkScope.CUSTOM_ORDER,
        cost_total=1600.0,
        master_total=1450.0,
        amount_from_client=8750.0,
    )
    assert studio == 5700.0


def test_kit_studio_profit_amount_without_client_amount() -> None:
    # без суммы с клиента студия = −ЗП мастеров
    assert kit_studio_profit_amount(
        scope=WorkScope.CUSTOM_ORDER,
        cost_total=1600.0,
        master_total=1450.0,
        amount_from_client=None,
    ) == -1450.0
    assert kit_studio_profit_amount(
        scope=WorkScope.CUSTOM_ORDER,
        cost_total=1600.0,
        master_total=1450.0,
        amount_from_client=0,
    ) == -1450.0


def test_kit_studio_profit_amount_in_stock_is_minus_masters() -> None:
    assert kit_studio_profit_amount(
        scope=WorkScope.IN_STOCK,
        cost_total=1600.0,
        master_total=1450.0,
        amount_from_client=None,
    ) == -1450.0
    # Даже если случайно указана сумма — считаем по ней (редкий кейс)
    assert kit_studio_profit_amount(
        scope=WorkScope.IN_STOCK,
        cost_total=1600.0,
        master_total=1450.0,
        amount_from_client=8750.0,
    ) == 5700.0
