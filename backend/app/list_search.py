"""Поиск в табличных списках (по ID)."""

from __future__ import annotations


def parse_list_id_search(raw: str | None) -> int | None:
    """Если строка — положительный номер (ID), вернуть его; иначе None."""
    s = (raw or "").strip().lstrip("#").strip()
    if not s or not s.isdigit():
        return None
    vid = int(s)
    return vid if vid > 0 else None
