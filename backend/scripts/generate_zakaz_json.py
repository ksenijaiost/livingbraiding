"""Генерация app/seed_data/zakaz_services.json. Запуск: python scripts/generate_zakaz_json.py"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "seed_data" / "zakaz_services.json"

rows: list[tuple[str, str, int, int | None]] = []


def add(sub: str, name: str, fr: int, to: int | None = None) -> None:
    rows.append((sub, name, fr, to))


# Комплект
add("Комплект", "Дредокудри", 7700, 8500)
add("Комплект", "Короткие косы 100шт", 6700, None)
add("Комплект", "Длинные косы 100шт", 7500, None)
add("Комплект", "Дреды 100шт", 10000, None)
add("Комплект", "Микро косы (200-240 шт)", 12000, None)

# Заготовки поштучно
add("Заготовки поштучно", "СЕ коса на заказ", 85, None)
add("Заготовки поштучно", "Се коса-кудря свободный кончик", 95, None)
add("Заготовки поштучно", "СЕ коса из наличия", 75, None)
add("Заготовки поштучно", "ДЕ коса / се ажурка", 150, None)
add("Заготовки поштучно", "Дред СЕ", 100, None)
add("Заготовки поштучно", "Дред Б/У", 150, None)
add("Заготовки поштучно", "Дред ДЕ", 200, None)
add("Заготовки поштучно", "Дредо кудря", 150, None)
add("Заготовки поштучно", "Дредо кудря Б/У", 80, 90)
add("Заготовки поштучно", "Коса б/у", 50, None)

# Резинки (полный список со 2-го скрина)
add("Резинки", "Прикрепление хвоста", 800, None)
add("Резинки", "Брейд под хвост", 1000, 1400)
add("Резинки", "Детская короткая", 500, 800)
add("Резинки", "Детская длинная", 800, 1300)
add("Резинки", "До плеч однотонная", 1600, None)
add("Резинки", "До плеч омбре", 2000, None)
add("Резинки", "До талии однотон", 2000, None)
add("Резинки", "До талии омбре", 2500, None)
add("Резинки", "До попы однотонная", 2500, None)
add("Резинки", "До попы омбре", 3000, None)
add("Резинки", "Ниже", 3000, 4000)

# Коррекция комплекта
add("Коррекция комплекта", "Чиканье 1 шт се-коса", 5, None)
add("Коррекция комплекта", "Чиканье Комплект 120 шт", 600, None)
add("Коррекция комплекта", "Одевание на круг", 100, None)
add("Коррекция комплекта", "Стирка", 400, None)
add("Коррекция комплекта", "Отпаривание", 200, None)
add("Коррекция комплекта", "Коррекция дреда", 50, None)
add("Коррекция комплекта", "Комплект дредов", 1000, 3000)
add("Коррекция комплекта", "Коррекция дредокудрей", 1000, 3000)

subs: OrderedDict[str, list] = OrderedDict()
for sub, name, fr, to in rows:
    price: dict = {"from": fr}
    if to is not None:
        price["to"] = to
    subs.setdefault(sub, []).append({"name": name, "price": price})

data = {"category": "Заказ", "subcategories": [{"name": k, "services": v} for k, v in subs.items()]}
OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print("Wrote", OUT, "rows", len(rows))
