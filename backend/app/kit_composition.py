"""Общая логика состава комплекта (ключи заготовок, JSON для composition_json)."""

from __future__ import annotations

import json
from typing import Any

# Стрижки в ЗП, но не учитываются в «количестве заготовок» для склада / предпросмотра.
KIT_INVENTORY_PIECE_EXCLUDE_KEYS: frozenset[str] = frozenset(
    {"SE_TRIM_SHORT", "SE_TRIM_LONG", "DE_TRIM"}
)


def composition_json_from_totals(kit_totals: dict[str, int]) -> str | None:
    """Тот же формат, что при создании комплекта из «Работа с товарами»."""
    items = [{"key": k, "qty": int(q)} for k, q in kit_totals.items() if int(q) > 0]
    return json.dumps(items, ensure_ascii=False) if items else None


def composition_json_from_lines(lines: list[Any]) -> str | None:
    from app.kit_composition_lines import lines_to_json

    return lines_to_json(lines)


def kit_inventory_piece_count(kit_totals: dict[str, int]) -> int:
    """Сумма количеств по ключам, входящим в складской учёт «штук комплекта»."""
    return sum(
        int(q)
        for k, q in kit_totals.items()
        if int(q) > 0 and k not in KIT_INVENTORY_PIECE_EXCLUDE_KEYS
    )
