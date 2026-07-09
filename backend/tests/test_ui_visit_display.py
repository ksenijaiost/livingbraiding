from __future__ import annotations

import json
from types import SimpleNamespace

from app.db.models import VisitMastersScope
from app.ui_visit_display import build_service_human_display, build_visit_master_pay_rows


def test_build_service_human_display_shows_custom_correction_amount() -> None:
    payload = {
        "kit": {
            "kind": "OWN",
            "own": {
                "origin": "FOREIGN",
                "correction": True,
                "extra_blanks": False,
                "correction_details": {
                    "trim_qty": 0,
                    "hourly_hours": 0.5,
                    "wash": False,
                    "steam": False,
                    "circle": False,
                    "use_custom_amount": True,
                    "custom_amount": 500.0,
                },
            },
        }
    }
    vs = SimpleNamespace(
        category_name="Вся голова",
        subcategory_name="Вплетение комплекта",
        service_name="В 2 руки",
        details_json=json.dumps(payload, ensure_ascii=False),
    )
    disp = build_service_human_display(vs)
    assert ("Корр.: своя сумма с клиента", "500 ₽") in disp["detail_blocks"]


def test_build_visit_master_pay_rows_visit_scope_splits_pool() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.VISIT,
        masters_pool=1000.0,
        mix_bonus_master_id=None,
        mix_bonus_amount=0.0,
        masters=[
            SimpleNamespace(master_id=1, percent=50.0, master=SimpleNamespace(display_name="Аня", username="a")),
            SimpleNamespace(master_id=2, percent=50.0, master=SimpleNamespace(display_name="Боря", username="b")),
        ],
        services=[
            SimpleNamespace(
                is_cancelled=False,
                masters_pool=1000.0,
                mix_bonus_master_id=None,
                mix_bonus_amount=0.0,
                masters=[],
            ),
        ],
    )
    rows = build_visit_master_pay_rows(visit)
    assert len(rows) == 2
    assert rows[0].master_name == "Аня"
    assert rows[0].total == 500.0
    assert rows[1].master_name == "Боря"
    assert rows[1].total == 500.0


def test_build_visit_master_pay_rows_per_service_and_mix_bonus() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.PER_SERVICE,
        masters_pool=800.0,
        mix_bonus_master_id=None,
        mix_bonus_amount=0.0,
        masters=[],
        services=[
            SimpleNamespace(
                is_cancelled=False,
                masters_pool=600.0,
                mix_bonus_master_id=2,
                mix_bonus_amount=100.0,
                masters=[
                    SimpleNamespace(master_id=1, percent=100.0, master=SimpleNamespace(display_name="Аня", username="a")),
                ],
            ),
            SimpleNamespace(
                is_cancelled=False,
                masters_pool=200.0,
                mix_bonus_master_id=None,
                mix_bonus_amount=0.0,
                masters=[
                    SimpleNamespace(master_id=2, percent=100.0, master=SimpleNamespace(display_name="Боря", username="b")),
                ],
            ),
        ],
    )
    rows = build_visit_master_pay_rows(visit)
    by_id = {r.master_id: r for r in rows}
    assert by_id[1].total == 600.0
    assert by_id[2].total == 300.0
    assert by_id[2].pool_share == 200.0
    assert by_id[2].mix_bonus == 100.0
