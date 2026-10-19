"""Российский номер клиента: хранение «7» + 10 цифр и показ «+7 (901) 123-45-67»."""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.engine import Connection

logger = logging.getLogger("alembic.runtime.migration")

INVALID_PHONE_MESSAGE = "Некорректный номер телефона"


class InvalidPhoneError(ValueError):
    def __init__(self) -> None:
        super().__init__(INVALID_PHONE_MESSAGE)


def normalize_phone(raw: str | None) -> str | None:
    """Пустая строка → None. Иначе «7» и 10 цифр, либо InvalidPhoneError."""
    if raw is None:
        return None
    text_value = str(raw).strip()
    if not text_value:
        return None
    digits = "".join(ch for ch in text_value if ch.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[-10:]
    if len(digits) == 10:
        return "7" + digits
    raise InvalidPhoneError()


def format_phone(normalized: str | None) -> str:
    """«79011234567» → «+7 (901) 123-45-67». Чужое значение возвращается как есть."""
    value = (normalized or "").strip()
    if len(value) != 11 or not value.isdigit() or not value.startswith("7"):
        return value
    return f"+7 ({value[1:4]}) {value[4:7]}-{value[7:9]}-{value[9:11]}"


def display_phone(phone_normalized: str | None, phone: str | None = None) -> str:
    if phone_normalized:
        shown = format_phone(phone_normalized)
        if shown:
            return shown
    return (phone or "").strip()


def tel_href(phone_normalized: str | None) -> str | None:
    """Ссылка tel:+7XXXXXXXXXX."""
    value = (phone_normalized or "").strip()
    if len(value) == 11 and value.isdigit() and value.startswith("7"):
        return "+" + value
    return None


def phone_search_digits(raw: str | None) -> str | None:
    """Цифры запроса для LIKE по phone_normalized.

    Меньше 4 цифр — номер не ищем. 4–11 цифр, первая 8, меняем её на 7,
    чтобы «8901» и полный «8901…» находили номер, записанный с семёрки.
    """
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) < 4:
        return None
    if digits[0] == "8" and len(digits) <= 11:
        digits = "7" + digits[1:]
    return digits


def backfill_client_phone_normalized(connection: Connection) -> list[tuple[int, str]]:
    """Заполнить phone_normalized. Нераспознанные номера остаются NULL и попадают в лог.

    Если один номер получился у нескольких клиентов, бросает RuntimeError до уникального индекса.
    """
    rows = connection.execute(text("SELECT id, phone FROM clients")).fetchall()
    unrecognized: list[tuple[int, str]] = []
    for client_id, phone in rows:
        original = "" if phone is None else str(phone)
        if not original.strip():
            continue
        try:
            normalized = normalize_phone(original)
        except InvalidPhoneError:
            logger.warning(
                "Клиент id=%s: телефон не распознан, phone_normalized оставлен пустым: %s",
                client_id,
                original,
            )
            unrecognized.append((int(client_id), original))
            continue
        connection.execute(
            text("UPDATE clients SET phone_normalized = :normalized WHERE id = :client_id"),
            {"normalized": normalized, "client_id": int(client_id)},
        )
    dup_rows = connection.execute(
        text(
            """
            SELECT phone_normalized, id
            FROM clients
            WHERE phone_normalized IS NOT NULL
            ORDER BY phone_normalized, id
            """
        )
    ).fetchall()
    grouped: dict[str, list[int]] = defaultdict(list)
    for normalized, client_id in dup_rows:
        grouped[str(normalized)].append(int(client_id))
    conflicts = {number: ids for number, ids in grouped.items() if len(ids) > 1}
    if conflicts:
        parts = [
            f"{number} ({format_phone(number)}): id {', '.join(str(item) for item in ids)}"
            for number, ids in sorted(conflicts.items())
        ]
        raise RuntimeError(
            "Нельзя сделать номер уникальным: один телефон у нескольких клиентов. " + "; ".join(parts)
        )
    return unrecognized
