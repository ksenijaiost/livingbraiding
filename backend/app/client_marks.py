"""Отметки клиента и отписка от акций.

no_promo блокирует только будущие рассылки, не напоминания о записи.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from app.forms_parse import parse_bool
from app.time_utils import utcnow_naive

NO_PROMO_SOURCE_MANUAL = "manual"
NO_PROMO_SOURCES = frozenset({"manual", "vk", "max", "telegram"})

# Необязательные булевы ключи массового импорта (промпт 5).
CLIENT_MARK_IMPORT_KEYS = (
    "no_promo",
    "interested_in_training",
    "is_student",
    "is_model",
)

NO_PROMO_SOURCE_LABELS = {
    "manual": "вручную",
    "vk": "VK",
    "max": "Max",
    "telegram": "Telegram",
}


def client_can_receive_promo(client: Any) -> bool:
    """Можно ли слать акции. Напоминания о записи этим не управляются."""
    return not bool(getattr(client, "no_promo", False))


def no_promo_source_label(source: str | None) -> str:
    raw = (source or "").strip().lower()
    if not raw:
        return ""
    return NO_PROMO_SOURCE_LABELS.get(raw, raw)


def apply_client_marks(
    client: Any,
    *,
    interested_in_training: bool,
    is_student: bool,
    is_model: bool,
    no_promo: bool,
    source: str = NO_PROMO_SOURCE_MANUAL,
    now: datetime | None = None,
) -> None:
    """Сохранить отметки. Снятие no_promo обнуляет дату и источник."""
    client.interested_in_training = bool(interested_in_training)
    client.is_student = bool(is_student)
    client.is_model = bool(is_model)
    want = bool(no_promo)
    if want:
        if not bool(getattr(client, "no_promo", False)):
            src = (source or NO_PROMO_SOURCE_MANUAL).strip().lower()
            if src not in NO_PROMO_SOURCES:
                src = NO_PROMO_SOURCE_MANUAL
            client.no_promo_at = now or utcnow_naive()
            client.no_promo_source = src
        client.no_promo = True
        return
    client.no_promo = False
    client.no_promo_at = None
    client.no_promo_source = None


def apply_client_import_marks(client: Any, row: Mapping[str, Any], *, now: datetime | None = None) -> None:
    """Применить необязательные булевы ключи импорта. Отсутствующий ключ не меняет поле."""
    if "interested_in_training" in row:
        client.interested_in_training = parse_bool(row.get("interested_in_training"))
    if "is_student" in row:
        client.is_student = parse_bool(row.get("is_student"))
    if "is_model" in row:
        client.is_model = parse_bool(row.get("is_model"))
    if "no_promo" not in row:
        return
    apply_client_marks(
        client,
        interested_in_training=bool(getattr(client, "interested_in_training", False)),
        is_student=bool(getattr(client, "is_student", False)),
        is_model=bool(getattr(client, "is_model", False)),
        no_promo=parse_bool(row.get("no_promo")),
        source=NO_PROMO_SOURCE_MANUAL,
        now=now,
    )
