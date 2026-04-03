"""Parse and validate client form fields (admin / master flows)."""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

from app.auth import AuthUser
from app.db.models import ClientAgeGroup

_CLIENT_SOURCES_PATH = Path(__file__).resolve().parent / "data" / "client_sources.json"


@lru_cache(maxsize=1)
def _client_source_items_cached() -> tuple[dict[str, str], ...]:
    with open(_CLIENT_SOURCES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("items") or []
    out: list[dict[str, str]] = []
    for it in items:
        out.append({"id": str(it["id"]), "label": str(it["label"])})
    return tuple(out)


def load_client_source_options() -> list[dict[str, str]]:
    """Labels from JSON справочника (редактируйте `app/data/client_sources.json`)."""
    return [dict(x) for x in _client_source_items_cached()]


def parse_client_source(raw: str | None) -> str | None:
    """Пусто или строго одна из подписей из справочника."""
    s = (raw or "").strip()
    if not s:
        return None
    allowed = {it["label"] for it in _client_source_items_cached()}
    if s not in allowed:
        raise ValueError("Выберите источник из списка.")
    return s[:120]


def format_created_by_label(user: AuthUser) -> str:
    return f"{user.display_name.strip()} ({user.role.value})"


def strip_or_none(raw: str | None, max_len: int | None = None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if max_len is not None and len(s) > max_len:
        s = s[:max_len]
    return s


def client_has_any_contact(
    phone: str | None,
    telegram: str | None,
    vk: str | None,
    instagram: str | None,
    other_contact: str | None,
) -> bool:
    return any(strip_or_none(x) for x in (phone, telegram, vk, instagram, other_contact))


def parse_optional_int(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        raise ValueError("Введите целое число для даты рождения.") from None


def parse_birth_fields(
    day_raw: str | None,
    month_raw: str | None,
    year_raw: str | None,
) -> tuple[int | None, int | None, int | None]:
    """
    Empty = all unknown.
    If day or month is set, both must be set (day–month or full date).
    Year alone is allowed.
    """
    try:
        d = parse_optional_int(day_raw)
        m = parse_optional_int(month_raw)
        y = parse_optional_int(year_raw)
    except ValueError as e:
        raise ValueError(str(e)) from e

    if d is None and m is None and y is None:
        return None, None, None

    if (d is None) != (m is None):
        raise ValueError("Укажите и день, и месяц (или оставьте оба пустыми).")

    if d is not None and m is not None:
        if not 1 <= m <= 12:
            raise ValueError("Месяц: от 1 до 12.")
        if not 1 <= d <= 31:
            raise ValueError("День: от 1 до 31.")

    if y is not None:
        cy = date.today().year
        if not 1900 <= y <= cy + 1:
            raise ValueError(f"Год: разумный диапазон (1900–{cy + 1}).")

    return d, m, y


def parse_age_group(raw: str | None) -> ClientAgeGroup | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return ClientAgeGroup(s)
    except ValueError:
        raise ValueError("Некорректная возрастная группа.") from None


# (form value, Russian label) for <select>
CLIENT_AGE_GROUP_OPTIONS: list[tuple[str, str]] = [
    ("", "— не указано —"),
    (ClientAgeGroup.U10.value, "До 10 лет"),
    (ClientAgeGroup.A10_18.value, "10–18 лет"),
    (ClientAgeGroup.A18_30.value, "18–30 лет"),
    (ClientAgeGroup.A30_50.value, "30–50 лет"),
    (ClientAgeGroup.A50P.value, "50+"),
]
