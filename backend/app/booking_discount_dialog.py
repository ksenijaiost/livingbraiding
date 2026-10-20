"""Информационные поля брони: скидка % и «где диалог» (без влияния на расчёты)."""

from __future__ import annotations

import re

_INT_RE = re.compile(r"^-?\d+$")
_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_LINK_HINTS = (
    "t.me/",
    "telegram.me/",
    "vk.com/",
    "vk.me/",
    "instagram.com/",
    "wa.me/",
    "whatsapp.com/",
    "max.ru/",
    "web.telegram.org",
)


def parse_booking_discount_percent(
    raw: object, *, required: bool
) -> tuple[int | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        if required:
            return None, "Укажите размер скидки."
        return None, None
    if not _INT_RE.fullmatch(text):
        return None, "Скидка должна быть целым числом от 0 до 100."
    value = int(text)
    if value < 0 or value > 100:
        return None, "Скидка должна быть целым числом от 0 до 100."
    return value, None


def parse_booking_dialog_location(
    raw: object, *, required: bool, max_len: int = 2000
) -> tuple[str | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        if required:
            return None, "Укажите, где диалог."
        return None, None
    if len(text) > max_len:
        return None, f"Текст не длиннее {max_len} символов."
    return text, None


def booking_discount_display(pct: int | None) -> str:
    if pct is None:
        return "не указано"
    if int(pct) == 0:
        return "без скидки"
    return f"{int(pct)}%"


def dialog_looks_like_link(raw: str | None) -> bool:
    text = (raw or "").strip()
    if not text:
        return False
    lower = text.casefold()
    if _URL_SCHEME_RE.match(text):
        return True
    return any(hint in lower for hint in _LINK_HINTS)


def dialog_href(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if _URL_SCHEME_RE.match(text):
        return text
    return "https://" + text
