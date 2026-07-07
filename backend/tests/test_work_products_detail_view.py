from __future__ import annotations

from types import SimpleNamespace

from app.db.models import MixSource, WorkKind, WorkScope
from app.work_products_detail_view import (
    build_composition_table_view,
    rubber_type_label,
    work_profit_explanation,
)


def test_rubber_type_label() -> None:
    assert "резинке" in rubber_type_label("TAIL_ELASTIC").lower()
    assert rubber_type_label("UNKNOWN_X") == "UNKNOWN_X"


def test_build_composition_table_view() -> None:
    staff = [
        SimpleNamespace(user_id=5, user=SimpleNamespace(display_name="Ира")),
    ]
    lines = [
        {
            "key": "SE_BRAID_LONG",
            "condition": "NEW",
            "by_staff": {"5": 3},
        },
        {
            "key": "SE_CURL",
            "condition": "USED",
            "used_price_pct": 50,
            "by_staff": {"5": 2},
        },
    ]
    table = build_composition_table_view(None, lines=lines, staff_rows=staff)
    assert table is not None
    assert table["grand_total"] == 5
    assert len(table["rows"]) == 2
    assert table["rows"][0]["total"] == 3
    assert table["rows"][1]["condition_label"] == "б/у"
    assert table["rows"][1]["used_price_pct"] == 50


def test_work_profit_explanation_kit_formula() -> None:
    work = SimpleNamespace(
        kind=WorkKind.KIT,
        scope=WorkScope.CUSTOM_ORDER,
        studio_share_snapshot=0.5,
        mix_source=MixSource.SELF_MIXED,
        amount_from_client=8750,
    )
    lines = work_profit_explanation(work, {"mix_complexity": "STANDARD", "kit": {"catalog_client_price": 7000}})
    text = " ".join(lines)
    assert "цена − себестоимость − ЗП мастера" in text.lower() or "цена − себестоимость" in text
    assert "8750" in text
    assert "совпадают" not in text
