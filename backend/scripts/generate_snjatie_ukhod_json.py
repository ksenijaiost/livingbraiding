"""Генерация app/seed_data/snjatie_ukhod_services.json. Запуск: python scripts/generate_snjatie_ukhod_json.py"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "seed_data" / "snjatie_ukhod_services.json"


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


snjatie: OrderedDict[str, list] = OrderedDict()

add(snjatie, "Расплетение", 'Снятие поштучно "точки"', 50, None)
add(snjatie, "Расплетение", "Расплетение полчаса", 800, 1000)
add(
    snjatie,
    "Расплетение",
    "Расплетение в 2 руки 1 час (если всего 1 час то минималка 1500₽)",
    1500,
    None,
)
add(snjatie, "Расплетение", "Расплетение 2 руки 1,5 часа", 2000, None)
add(snjatie, "Расплетение", "Расплетение в 2 руки 2 часа", 2500, None)
add(snjatie, "Расплетение", "Расплетение в 2 руки 2,5 часа", 2750, None)
add(snjatie, "Расплетение", "Расплетение в 2 руки 3 часа", 3000, None)
add(snjatie, "Расплетение", "Расплетение 4 руки полчаса", 1500, None)
add(snjatie, "Расплетение", "Расплетение в 4 руки 1 час", 2000, None)
add(snjatie, "Расплетение", "Расплетение в 4 руки (за 1,5 часа)", 2500, None)
add(snjatie, "Расплетение", "Расплетение в 4 руки (2 часа)", 3000, None)
add(snjatie, "Расплетение", "Расплетение в 4 руки (2,5 часа)", 3500, None)

add(
    snjatie,
    "Снятие наращивания",
    "Снятие капсул поштучно (вычесывание отдельно)",
    10,
    15,
)
add(snjatie, "Снятие наращивания", "Снятие наращивания 0,5 ч 2 руки", 800, None)
add(snjatie, "Снятие наращивания", "Снятие наращивания 0,5 ч 4 руки", 1200, None)
add(snjatie, "Снятие наращивания", "Снятие наращивания 1 ч 2 руки", 1500, None)
add(snjatie, "Снятие наращивания", "Снятие наращивания 1 ч 4 руки", 2000, None)
add(snjatie, "Снятие наращивания", "Снятие наращивания 1,5 ч 2 руки", 2000, None)
add(snjatie, "Снятие наращивания", "Снятие наращивания 1,5 ч 4 руки", 2800, None)
add(snjatie, "Снятие наращивания", "Снятие наращивания 2,5 ч 2 руки", 2500, None)

ukhod: OrderedDict[str, list] = OrderedDict()

add(ukhod, "Мытьё", "Мытьё с пилингом или с плазмой", 800, None)
add(ukhod, "Мытьё", "Мытьё в косах на 2 раза", 1000, None)
add(ukhod, "Мытьё", "Мытьё волос", 500, None)
add(
    ukhod,
    "Коррекция",
    "Коррекция краевой 13-15 баз (считаем поштучно 120-150Р) за базу",
    150,
    None,
)
add(ukhod, "Другое", "Сушка феном", 300, None)
add(ukhod, "Другое", "Стрижка", 500, None)
add(ukhod, "Другое", "Разбор ( уход) 1000Р в час", 1000, None)

bundle = {
    "catalogs": [
        {
            "category": "Снятие",
            "subcategories": [{"name": k, "services": v} for k, v in snjatie.items()],
        },
        {
            "category": "Уход",
            "subcategories": [{"name": k, "services": v} for k, v in ukhod.items()],
        },
    ]
}
OUT.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
n1 = sum(len(v) for v in snjatie.values())
n2 = sum(len(v) for v in ukhod.values())
print("Wrote", OUT, "Снятие:", len(snjatie), "подкат.", n1, "усл.; Уход:", len(ukhod), "подкат.", n2, "усл.")
