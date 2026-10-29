"""Информационные поля брони: скидка %, «где диалог», озвученная сумма (без влияния на расчёты)."""

from __future__ import annotations

import re
from typing import Any

_INT_RE = re.compile(r"^-?\d+$")
_LINE_SERVICE_ID_RE = re.compile(r"^line_(\d+)_service_id$")
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


def parse_booking_quoted_price_text(
    raw: object, *, required: bool, max_len: int = 120
) -> tuple[str | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        if required:
            return None, "Укажите сумму, озвученную клиенту."
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


BOOKING_DISCOUNT_HINT_KEY = "booking_discount_percent_hint"


def apply_booking_discount_to_visit_prefill(fp: dict[str, Any], booking: Any) -> None:
    """Подставить скидку брони в форму визита/черновика, если ещё не задана.

    Если bookings.discount_percent is NULL — ничего не делает.
    Подсказка «Из брони: N%» пишется в booking_discount_percent_hint и не сбрасывается
    при ручном изменении client_discount_percent.
    """
    raw_pct = getattr(booking, "discount_percent", None)
    if raw_pct is None:
        return
    pct = str(int(raw_pct))
    fp[BOOKING_DISCOUNT_HINT_KEY] = pct
    current = fp.get("client_discount_percent")
    if current is None or str(current).strip() == "":
        fp["client_discount_percent"] = pct
    for key in list(fp.keys()):
        match = _LINE_SERVICE_ID_RE.match(str(key))
        if not match:
            continue
        idx = int(match.group(1))
        if idx < 1:
            continue
        line_key = f"line_{idx}_client_discount_percent"
        line_val = fp.get(line_key)
        if line_val is None or str(line_val).strip() == "":
            fp[line_key] = pct
