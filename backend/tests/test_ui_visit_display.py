from __future__ import annotations

import json
from types import SimpleNamespace

from app.db.models import VisitMastersScope
from app.ui_visit_display import build_service_human_display, build_visit_master_pay_rows, build_visit_masters_lines


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
                sort_order=0,
                id=1,
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
    assert rows[0].breakdown_paren == "1. 500"
    assert rows[1].master_name == "Боря"
    assert rows[1].total == 500.0


def test_build_visit_master_pay_rows_per_service_ignores_mix_bonus() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.PER_SERVICE,
        masters_pool=800.0,
        mix_bonus_master_id=None,
        mix_bonus_amount=0.0,
        masters=[],
        services=[
            SimpleNamespace(
                is_cancelled=False,
                sort_order=0,
                id=1,
                masters_pool=600.0,
                mix_bonus_master_id=2,
                mix_bonus_amount=100.0,
                masters=[
                    SimpleNamespace(master_id=1, percent=100.0, master=SimpleNamespace(display_name="Аня", username="a")),
                ],
            ),
            SimpleNamespace(
                is_cancelled=False,
                sort_order=1,
                id=2,
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
    assert by_id[1].breakdown_paren == "1. 600"
    assert by_id[2].total == 200.0
    assert by_id[2].pool_share == 200.0
    assert by_id[2].mix_bonus == 0.0
    assert by_id[2].breakdown_paren == "2. 200"


def test_build_visit_master_pay_rows_service_breakdown_and_help() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.PER_SERVICE,
        masters_pool=2700.0,
        hourly_help_json=json.dumps(
            [{"master_id": 2, "hours": 0, "minutes": 30, "amount": 250.0}],
            ensure_ascii=False,
        ),
        mix_bonus_master_id=None,
        mix_bonus_amount=0.0,
        masters=[],
        services=[
            SimpleNamespace(
                is_cancelled=False,
                sort_order=0,
                id=10,
                masters_pool=400.0,
                masters=[
                    SimpleNamespace(master_id=1, percent=100.0, master=SimpleNamespace(display_name="Юля", username="y")),
                ],
            ),
            SimpleNamespace(
                is_cancelled=False,
                sort_order=1,
                id=11,
                masters_pool=2000.0,
                masters=[
                    SimpleNamespace(master_id=1, percent=100.0, master=SimpleNamespace(display_name="Юля", username="y")),
                ],
            ),
            SimpleNamespace(
                is_cancelled=False,
                sort_order=2,
                id=12,
                masters_pool=300.0,
                masters=[
                    SimpleNamespace(master_id=1, percent=100.0, master=SimpleNamespace(display_name="Юля", username="y")),
                ],
            ),
        ],
    )
    rows = build_visit_master_pay_rows(visit)
    by_id = {r.master_id: r for r in rows}
    assert by_id[1].total == 2700.0
    assert by_id[1].breakdown_paren == "1. 400, 2. 2000, 3. 300"
    assert by_id[2].total == 250.0
    assert by_id[2].breakdown_paren == "помощь 250"


def test_build_visit_master_pay_rows_includes_hourly_help() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.VISIT,
        masters_pool=700.0,
        hourly_help_json=json.dumps(
            [{"master_id": 3, "hours": 1, "minutes": 0, "amount": 300.0}],
            ensure_ascii=False,
        ),
        mix_bonus_master_id=None,
        mix_bonus_amount=0.0,
        masters=[
            SimpleNamespace(master_id=1, percent=100.0, master=SimpleNamespace(display_name="Аня", username="a")),
        ],
        services=[
            SimpleNamespace(
                is_cancelled=False,
                sort_order=0,
                id=1,
                masters_pool=700.0,
                mix_bonus_master_id=None,
                mix_bonus_amount=0.0,
                masters=[],
            ),
        ],
    )
    rows = build_visit_master_pay_rows(visit)
    by_id = {r.master_id: r for r in rows}
    assert by_id[1].total == 700.0
    assert by_id[1].hourly_help == 0.0
    assert by_id[3].total == 300.0
    assert by_id[3].hourly_help == 300.0
    assert by_id[3].pool_share == 0.0


def test_build_visit_masters_lines_per_service() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.PER_SERVICE,
        masters=[],
        services=[
            SimpleNamespace(
                is_cancelled=False,
                sort_order=0,
                id=10,
                masters_pool=1000.0,
                masters=[
                    SimpleNamespace(
                        master_id=1,
                        id=1,
                        percent=50.0,
                        master=SimpleNamespace(display_name="Ira", username="ira"),
                    ),
                    SimpleNamespace(
                        master_id=2,
                        id=2,
                        percent=50.0,
                        master=SimpleNamespace(display_name="Yulya", username="yulya"),
                    ),
                ],
            ),
            SimpleNamespace(
                is_cancelled=False,
                sort_order=1,
                id=11,
                masters_pool=400.0,
                masters=[
                    SimpleNamespace(
                        master_id=1,
                        id=3,
                        percent=100.0,
                        master=SimpleNamespace(display_name="Ira", username="ira"),
                    ),
                ],
            ),
        ],
    )
    lines = build_visit_masters_lines(visit)
    assert len(lines) == 2
    assert lines[0].service_number == 1
    assert "Ira (50%, 500 ₽)" in lines[0].masters_text
    assert "Yulya (50%, 500 ₽)" in lines[0].masters_text
    assert lines[1].service_number == 2
    assert lines[1].masters_text == "Ira (100%, 400 ₽)"


def test_build_visit_masters_lines_visit_scope() -> None:
    visit = SimpleNamespace(
        masters_scope=VisitMastersScope.VISIT,
        masters_pool=800.0,
        masters=[
            SimpleNamespace(
                master_id=1,
                id=1,
                percent=100.0,
                master=SimpleNamespace(display_name="Ira", username="ira"),
            ),
        ],
        services=[
            SimpleNamespace(is_cancelled=False, sort_order=0, id=10, masters_pool=800.0, masters=[]),
        ],
    )
    lines = build_visit_masters_lines(visit)
    assert len(lines) == 1
    assert lines[0].masters_text == "Ira (100%, 800 ₽)"
