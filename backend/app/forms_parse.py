from __future__ import annotations

from datetime import date, datetime
from typing import Any


def parse_bool(raw: Any) -> bool:
    """Checkbox / `on` / `1` / `true` / `yes` (и явный bool)."""
    if isinstance(raw, bool):
        return raw
    s = str(raw or "").strip().lower()
    return s in ("1", "true", "on", "yes")


def parse_int(
    raw: str | int | None,
    *,
    min: int | None = None,
    max: int | None = None,
    default: int | None = None,
    field_name: str = "value",
) -> int:
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        if default is not None:
            v: int = int(default)
        else:
            raise ValueError(f"{field_name}: пустое значение")
    else:
        try:
            v = int(s)
        except ValueError as e:
            raise ValueError(f"{field_name}: ожидается целое число") from e
    if min is not None and v < min:
        raise ValueError(f"{field_name}: не меньше {min}")
    if max is not None and v > max:
        raise ValueError(f"{field_name}: не больше {max}")
    return v


def parse_float(
    raw: str | int | float | None,
    *,
    min: float | None = None,
    max: float | None = None,
    default: float | None = None,
    field_name: str = "value",
) -> float:
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        if default is not None:
            v = float(default)
        else:
            raise ValueError(f"{field_name}: пустое значение")
    else:
        s = s.replace(",", ".")
        try:
            v = float(s)
        except ValueError as e:
            raise ValueError(f"{field_name}: ожидается число") from e
    if min is not None and v < min:
        raise ValueError(f"{field_name}: не меньше {min}")
    if max is not None and v > max:
        raise ValueError(f"{field_name}: не больше {max}")
    return v


def parse_optional_float(
    raw: str | int | float | None,
    *,
    min: float | None = None,
    max: float | None = None,
    field_name: str = "value",
) -> float | None:
    """Пустая строка → `None`, иначе `parse_float`."""
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        return None
    return parse_float(s, min=min, max=max, field_name=field_name)


def parse_date_iso(raw: str | None, *, field_name: str = "date") -> date:
    """Дата в формате `YYYY-MM-DD` (как `date.fromisoformat`)."""
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        raise ValueError(f"{field_name}: пустая дата")
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"{field_name}: ожидается дата YYYY-MM-DD") from e


def parse_date_form(raw: str | None, *, field_name: str = "date") -> date:
    """Дата из формы: ISO `YYYY-MM-DD` или `ДД.ММ.ГГГГ` / `ДД/ММ/ГГГГ`."""
    s = ("" if raw is None else str(raw)).strip()
    if not s:
        raise ValueError(f"{field_name}: пустая дата")
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{field_name}: ожидается дата YYYY-MM-DD или ДД.ММ.ГГГГ")
