from __future__ import annotations

import json
from types import SimpleNamespace

from app.ui_visit_display import build_service_human_display


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
