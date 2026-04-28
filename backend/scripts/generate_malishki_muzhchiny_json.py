"""Генерация app/seed_data/malishki_muzhchiny_services.json. Запуск: python scripts/generate_malishki_muzhchiny_json.py"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "seed_data" / "malishki_muzhchiny_services.json"

# Подкатегория-прочерк для «Малышки» (в БД подкатегория обязательна)
DASH_SUB = "—"

malishki_services = [
    ("Корона 2-3 брейда", 1400, 2000),
    ("Сфинкс из брейдов", 4000, 5000),
    ("Две косы с височными", 2400, None),
    ("Четыре брейда", 2800, None),
]

muzhchiny = [
    ("Классика (точка)", "Афрокосы на андеркат поштучно", 150, None),
    ("Классика (точка)", "Де-Дреды изготовление на андеркат до плеч 100", 150, None),
    ("Классика (точка)", "Полу8 поштучно (без учета комплекта)", 100, None),
    ("Классика (точка)", "Стрижка под андеркат", 500, None),
    ("Брейды", "Брейды на андеркат поштучно", 500, 600),
    ("Брейды", "Брейды по всей голове", 3500, 6000),
    ("Уход", "Мытье с пилингом (короткий волос)", 400, None),
]


def price_obj(fr: int, to: int | None) -> dict:
    d: dict = {"from": fr}
    if to is not None:
        d["to"] = to
    return d


malishki_block = {
    "category": "Малышки 3-7л",
    "subcategories": [
        {
            "name": DASH_SUB,
            "services": [{"name": n, "price": price_obj(a, b)} for n, a, b in malishki_services],
        }
    ],
}

from collections import OrderedDict

m_subs: OrderedDict[str, list] = OrderedDict()
for sub, name, fr, to in muzhchiny:
    m_subs.setdefault(sub, []).append({"name": name, "price": price_obj(fr, to)})

muzhchiny_block = {
    "category": "Мужчины",
    "subcategories": [{"name": k, "services": v} for k, v in m_subs.items()],
}

bundle = {"catalogs": [malishki_block, muzhchiny_block]}
OUT.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
print("Wrote", OUT)
