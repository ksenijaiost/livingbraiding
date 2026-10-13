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
    ("confirm_1", "Подтверждение (1)"),
    ("reschedule_2", "Перенос (2)"),
    ("cancel_3", "Отмена (3)"),
    ("contact_vk", "Связь с админом — VK (4)"),
    ("contact_telegram", "Связь с админом — Telegram (4)"),
    ("contact_max", "Связь с админом — Max (4)"),
    ("directions_5", "Как добраться (5)"),
    ("hint", "Непонятный ответ"),
    ("no_booking", "Нет активной записи"),
    ("no_booking_reschedule", "Нет активной записи при переносе (2)"),
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
    ensure_bot_reply_templates(db)
    return [{"key": key, "label": label, "body": bot_reply_body(db, key)} for key, label in BOT_REPLY_LABELS]


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
