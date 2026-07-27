from __future__ import annotations

from types import SimpleNamespace

from app.price_ordering import price_sort_key, service_sort_price


def test_service_sort_price_uses_lowest_available_value() -> None:
    svc = SimpleNamespace(
        price_junior_from=None,
        price_junior_to=3200.0,
        price_middle_from=2800.0,
        price_middle_to=3500.0,
        price_senior_from=4000.0,
        price_senior_to=None,
    )
    assert service_sort_price(svc) == 2800.0


def test_price_sort_key_puts_empty_prices_last() -> None:
    rows = [
        SimpleNamespace(name="Без цены", price=None),
        SimpleNamespace(name="Дороже", price=2500.0),
        SimpleNamespace(name="Дешевле", price=1500.0),
    ]

    ordered = sorted(rows, key=lambda r: price_sort_key(r.price, name=r.name))

    assert [r.name for r in ordered] == ["Дешевле", "Дороже", "Без цены"]
