"""Генерация app/seed_data/narashivanie_services.json. Запуск: python scripts/generate_narashivanie_json.py"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "seed_data" / "narashivanie_services.json"


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

# Подкатегория LED
add(subs, "LED", "Каждая зона 1-2 см в комплексе с теплым наращиванием", 500, None)
add(subs, "LED", "Каждая зона 3-4 см в комплексе с теплым наращиванием", 1000, None)
add(subs, "LED", "Пряди у лица 1-2 рядка", 1600, 2000)
add(subs, "LED", "Двойные пряди у лица 3-4 рядка", 2800, None)
add(
    subs,
    "LED",
    "Челка/затылок/виски (площадь 2-5 см) или пробор без учета затылка 1 ряд",
    3800,
    None,
)
add(subs, "LED", "Челка (площадь 2-5 см) + пробор без учета затылка", 4800, None)
add(
    subs,
    "LED",
    "Челка/затылок/виски (площадь 6-8 см) или пробор включая затылок 1,5 ряда",
    5800,
    None,
)
add(
    subs,
    "LED",
    "Челка/затылок/виски (площадь 6-8 см) + пробор включая затылок 1 ряд",
    6800,
    None,
)
add(
    subs,
    "LED",
    "Челка/затылок/виски (площадь 6-8 см) + пробор включая затылок 1,5-2 ряда",
    7800,
    None,
)
add(subs, "LED", "Загущение с удлинением до 20см", 10000, 15000)
add(subs, "LED", "Вся голова", 20000, 25000)

add(subs, "Микрокосы", "Наращивание 1 шт", 600, None)
add(subs, "Микрокосы", "Наращивание 2 шт", 800, None)
add(subs, "Микрокосы", "Наращивание поштучно 4-6 шт (цена за 1шт)", 300, None)
add(subs, "Микрокосы", "Наращивание поштучно от 7шт", 250, None)
add(subs, "Микрокосы", "1 ряд цветной", 3900, None)
add(subs, "Микрокосы", "1 ряд натуральный под цвет волос", 4900, None)
add(subs, "Микрокосы", "Полное наращивание", 6500, 10000)

add(subs, "Трессы", "Наращивание на трессы", 4000, 4500)

add(subs, "Тёплое", "Покапсульно", 50, 75)
add(subs, "Тёплое", "Загущение челка/виски", 2500, 5500)
add(subs, "Тёплое", "Загущение без удлинения", 4000, 5500)
add(subs, "Тёплое", "Загущение с удлинением", 7000, 8500)
add(subs, "Тёплое", "Вся голова экспресс", 10000, 12500)
add(subs, "Тёплое", "Вся голова полное", 9500, 16500)

bundle = {
    "catalogs": [
        {
            "category": "Наращивание",
            "subcategories": [{"name": k, "services": v} for k, v in subs.items()],
        }
    ]
}
OUT.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
n = sum(len(v) for v in subs.values())
print("Wrote", OUT, "subcategories", len(subs), "services", n)
