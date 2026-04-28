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


class _Svc:
    def __init__(self, *, retail_material_kanekalon: bool, retail_material_kudri: bool):
        self.retail_material_kanekalon = retail_material_kanekalon
        self.retail_material_kudri = retail_material_kudri


class _Sale:
    def __init__(self):
        self.kind = None
        self.material_kanekalon_price_per_gram_at_time = "sentinel"
        self.material_kudri_price_per_gram_at_time = "sentinel"


def test_apply_price_snapshots_sets_only_enabled_fields() -> None:
    from app.db.models import ProductSaleKind
    from app.product_sale_material import apply_price_snapshots

    sale = _Sale()
    sale.kind = ProductSaleKind.MATERIAL
    svc = _Svc(retail_material_kanekalon=True, retail_material_kudri=False)
    db = _FakeDB(k=1.5, ku=9.9)

    apply_price_snapshots(db, sale, svc)
    assert sale.material_kanekalon_price_per_gram_at_time == 1.5
    assert sale.material_kudri_price_per_gram_at_time is None


def test_apply_price_snapshots_is_noop_for_non_material_sale() -> None:
    from app.db.models import ProductSaleKind
    from app.product_sale_material import apply_price_snapshots

    sale = _Sale()
    sale.kind = ProductSaleKind.OTHER
    svc = _Svc(retail_material_kanekalon=True, retail_material_kudri=True)
    db = _FakeDB(k=2.0, ku=3.0)

    apply_price_snapshots(db, sale, svc)
    # unchanged
    assert sale.material_kanekalon_price_per_gram_at_time == "sentinel"
    assert sale.material_kudri_price_per_gram_at_time == "sentinel"
