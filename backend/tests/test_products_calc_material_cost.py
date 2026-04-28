from __future__ import annotations


class _Price:
    def __init__(self, price_per_gram: float):
        self.price_per_gram = price_per_gram


class _FakeDB:
    def __init__(self, *, k: float | None, ku: float | None):
        self._k = k
        self._ku = ku

    def get(self, model, key):  # noqa: ANN001
        from app.db.models import MaterialType

        if key == MaterialType.KANEKALON:
            return _Price(self._k) if self._k is not None else None
        if key == MaterialType.KUDRI:
            return _Price(self._ku) if self._ku is not None else None
        return None


def test_material_cost_total_uses_prices_and_clamps_negative_grams() -> None:
    from app.routes.products_calc import _material_cost_total

    db = _FakeDB(k=2.0, ku=3.0)
    # negative grams should clamp to 0
    got = _material_cost_total(db, kanekalon_grams=-10, kudri_grams=5)
    assert got == 15.0


def test_material_cost_total_returns_zero_when_no_prices() -> None:
    from app.routes.products_calc import _material_cost_total

    db = _FakeDB(k=None, ku=None)
    got = _material_cost_total(db, kanekalon_grams=100, kudri_grams=50)
    assert got == 0.0
