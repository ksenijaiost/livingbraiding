"""Тексты ответов бота клиенту. Подставляются переменные студии."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ClientBotReplyTemplate
from app.settings import get_settings
from app.time_utils import utcnow_naive

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")
_MAX_BODY = 4000

BOT_REPLY_DEFAULTS: dict[str, str] = {
    "confirm_1": "Спасибо, запись подтверждена",
    "reschedule_2": "Передали администратору, с вами свяжутся, чтобы подобрать другое время",
    "cancel_3": "Передали администратору, с вами свяжутся",
    "contact_vk": "Администратор скоро напишет вам здесь",
    "contact_telegram": "Напишите нам: {{studio_telegram}}",
    "contact_max": "Напишите нам: {{studio_max}}",
    "directions_5": "Как добраться: {{address}}\n{{route_video}}",
    "hint": (
        "Ответьте: 1 — подтвердить, 2 — перенести, 3 — отменить, "
        "4 — написать администратору, 5 — как добраться"
    ),
    "no_booking": "Не нашли активную запись, свяжитесь с нами",
    "no_booking_reschedule": "Не нашли активную запись, напишите 4, чтобы связаться с администратором",
}

BOT_REPLY_LABELS: tuple[tuple[str, str], ...] = (
    ("confirm_1", "Подтверждение"),
    ("reschedule_2", "Перенос"),
    ("cancel_3", "Отмена"),
    ("contact_vk", "Связь с админом — VK"),
    ("contact_telegram", "Связь с админом — Telegram"),
    ("contact_max", "Связь с админом — Max"),
    ("directions_5", "Как добраться"),
    ("hint", "Непонятный ответ (если не используете автоподсказку)"),
    ("no_booking", "Нет активной записи"),
    ("no_booking_reschedule", "Нет активной записи при переносе"),
)

_CONTACT_KEY = {
    "vk": "contact_vk",
    "telegram": "contact_telegram",
    "max": "contact_max",
}


def ensure_bot_reply_templates(db: Session) -> bool:
    have = set(db.scalars(select(ClientBotReplyTemplate.key)).all())
    created = False
    now = utcnow_naive()
    for key, body in BOT_REPLY_DEFAULTS.items():
        if key in have:
            continue
        db.add(ClientBotReplyTemplate(key=key, body=body, updated_at=now))
        created = True
    if created:
        db.flush()
    return created


def bot_reply_body(db: Session, key: str) -> str:
    row = db.get(ClientBotReplyTemplate, key)
    if row is None or not (row.body or "").strip():
        return BOT_REPLY_DEFAULTS.get(key, "")
    return row.body


def render_bot_reply(db: Session, key: str) -> str:
    from app.route_video_send import strip_route_video_placeholder
    from app.studio_template_variables import studio_template_values

    text = strip_route_video_placeholder(bot_reply_body(db, key))

    studio = studio_template_values(db)

    def _repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in studio:
            return studio[name]
        return m.group(0)

    return _VAR_RE.sub(_repl, text)


def contact_reply_key(channel: str) -> str:
    return _CONTACT_KEY.get(channel.strip().lower(), "contact_vk")


def bot_replies_for_ui(db: Session) -> list[dict[str, str]]:
    from app.client_reply_digits import (
        ACTION_CANCEL,
        ACTION_CONFIRM,
        ACTION_CONTACT,
        ACTION_DIRECTIONS,
        ACTION_RESCHEDULE,
        digit_for_action,
        ensure_default_reply_digits,
    )

    ensure_bot_reply_templates(db)
    ensure_default_reply_digits(db)
    digit_suffix = {
        "confirm_1": digit_for_action(db, ACTION_CONFIRM),
        "reschedule_2": digit_for_action(db, ACTION_RESCHEDULE),
        "cancel_3": digit_for_action(db, ACTION_CANCEL),
        "contact_vk": digit_for_action(db, ACTION_CONTACT),
        "contact_telegram": digit_for_action(db, ACTION_CONTACT),
        "contact_max": digit_for_action(db, ACTION_CONTACT),
        "directions_5": digit_for_action(db, ACTION_DIRECTIONS),
        "no_booking_reschedule": digit_for_action(db, ACTION_RESCHEDULE),
    }
    out: list[dict[str, str]] = []
    for key, label in BOT_REPLY_LABELS:
        digit = digit_suffix.get(key)
        shown = f"{label} ({digit})" if digit else label
        out.append({"key": key, "label": shown, "body": bot_reply_body(db, key)})
    return out


def save_bot_reply_templates(db: Session, values: dict[str, str], *, user_id: int | None) -> None:
    ensure_bot_reply_templates(db)
    now = utcnow_naive()
    for key, _label in BOT_REPLY_LABELS:
        if key not in values:
            continue
        body = (values[key] or "").strip()
        if not body:
            raise ValueError(f"Текст «{_label_of(key)}» не может быть пустым.")
        if len(body) > _MAX_BODY:
            raise ValueError(f"Текст «{_label_of(key)}» слишком длинный.")
        row = db.get(ClientBotReplyTemplate, key)
        if row is None:
            db.add(
                ClientBotReplyTemplate(
                    key=key, body=body, updated_at=now, updated_by_user_id=user_id
                )
            )
        else:
            row.body = body
            row.updated_at = now
            row.updated_by_user_id = user_id
    db.flush()


def _label_of(key: str) -> str:
    for k, label in BOT_REPLY_LABELS:
        if k == key:
            return label
    return key


def vk_community_dialog_url(vk_user_id: int) -> str | None:
    """Ссылка на диалог в сообществе: https://vk.com/gim<group_id>?sel=<vk_user_id>."""
    raw = (get_settings().vk_group_id or "").strip()
    if not raw:
        return None
    digits = raw.lstrip("-")
    if not digits.isdigit() or int(digits) <= 0:
        return None
    return f"https://vk.com/gim{int(digits)}?sel={int(vk_user_id)}"
