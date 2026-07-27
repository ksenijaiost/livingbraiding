from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _clean_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def service_sort_price(service: Any) -> float | None:
    """Минимальная доступная цена услуги для сортировки в прайсе."""
    prices = [
        _clean_number(getattr(service, "price_junior_from", None)),
        _clean_number(getattr(service, "price_junior_to", None)),
        _clean_number(getattr(service, "price_middle_from", None)),
        _clean_number(getattr(service, "price_middle_to", None)),
        _clean_number(getattr(service, "price_senior_from", None)),
        _clean_number(getattr(service, "price_senior_to", None)),
    ]
    present = [p for p in prices if p is not None]
    if not present:
        return None
    return min(present)


def price_sort_key(
    price: float | None,
    *,
    name: str | None = None,
    secondary: Iterable[Any] = (),
) -> tuple[Any, ...]:
    clean = _clean_number(price)
    return (
        clean is None,
        clean if clean is not None else 0.0,
        str(name or "").strip().lower(),
        *secondary,
    )
