"""Генерация app/seed_data/prodazha_materiala_services.json. Запуск: python scripts/generate_prodazha_materiala_json.py"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "seed_data" / "prodazha_materiala_services.json"


def add(
    subs: OrderedDict[str, list],
    sub: str,
    name: str,
    fr: int | None,
    to: int | None = None,
) -> None:
    price: dict = {}
    if fr is not None:
        price["from"] = fr
    if to is not None:
        price["to"] = to
    entry: dict = {"name": name}
    if price:
        entry["price"] = price
    subs.setdefault(sub, []).append(entry)


subs: OrderedDict[str, list] = OrderedDict()

add(subs, "Канекалон КИТАЙ JUMBO", "Покупатель", 250, None)
add(subs, "Канекалон КИТАЙ JUMBO", "Ученикам", 200, None)
add(subs, "Канекалон КИТАЙ JUMBO", "Новая палитра", 300, None)

add(subs, "Канекалон X-PRESSION", "Однотонный", 400, None)
add(subs, "Канекалон X-PRESSION", "Полпачки", 250, None)
add(subs, "Канекалон X-PRESSION", "Омбре", 500, 550)
add(subs, "Канекалон X-PRESSION", "Ученикам", 350, None)

add(subs, "Канекалон EASY BRAID", "Покупатель", 400, None)
add(subs, "Канекалон EASY BRAID", "Ученикам", 350, None)
add(subs, "Канекалон EASY BRAID", "Изик новая палитра", 400, None)

add(subs, "Кудри", "Бразильки", 500, None)
add(subs, "Кудри", "Ариэль 100 гр", 800, None)
add(subs, "Кудри", "Анна 100 гр", 900, None)
add(subs, "Кудри", "Боди", 800, None)

add(subs, "Другое", "АИДА 100 гр.", 450, None)
add(subs, "Другое", "ЗИЗИ", 400, None)
add(subs, "Другое", "Сенегалки", 500, None)
add(subs, "Другое", "Термоволокно", 800, 1000)
add(subs, "Другое", "Смешанный готовый материал за 100 гр.", 500, None)
add(subs, "Другое", "«На развес» любой канекалон за 100 гр.", 400, None)
add(subs, "Другое", "Ученикам за 100 гр. на развес изик", 350, None)

bundle = {
    "catalogs": [
        {
            "category": "Продажа материала",
            "include_in_visit": False,
            "subcategories": [{"name": k, "services": v} for k, v in subs.items()],
        }
    ]
}
OUT.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
n = sum(len(v) for v in subs.values())
print("Wrote", OUT, "subcategories", len(subs), "items", n)
