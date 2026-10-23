"""Общие чаты студии: /admins и /general, хранение chat_id, тест/отключение."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import AdminChatLinkToken, AdminChatTarget, NotificationChannel
from app.notifications import send_max, send_telegram, send_vk
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

LINK_TTL = timedelta(hours=24)
CHAT_KIND_ADMIN = "admin"
CHAT_KIND_GENERAL = "general"
_CHAT_KINDS = (CHAT_KIND_ADMIN, CHAT_KIND_GENERAL)

MSG_ADMIN_CHAT_LINKED = "Чат админов подключён"
MSG_ADMIN_CHAT_UNLINKED = "Чат админов отключён"
MSG_ADMIN_CHAT_INVALID = "Код устарел, запросите новый в CRM"
MSG_ADMIN_CHAT_TEST = "Тест: чат админов Живём Плетём"
MSG_GENERAL_CHAT_LINKED = "Общий чат подключён"
MSG_GENERAL_CHAT_UNLINKED = "Общий чат отключён"
MSG_GENERAL_CHAT_TEST = "Тест: общий чат Живём Плетём"

MSG_ADMINS_USAGE = (
    "Отправьте команду в виде: /admins КОД. "
    "Код берите в CRM (Студия → Управление уведомлениями)."
)
MSG_GENERAL_USAGE = (
    "Отправьте команду в виде: /general КОД. "
    "Код берите в CRM (Студия → Управление уведомлениями)."
)

_LINKED = {CHAT_KIND_ADMIN: MSG_ADMIN_CHAT_LINKED, CHAT_KIND_GENERAL: MSG_GENERAL_CHAT_LINKED}
_UNLINKED = {CHAT_KIND_ADMIN: MSG_ADMIN_CHAT_UNLINKED, CHAT_KIND_GENERAL: MSG_GENERAL_CHAT_UNLINKED}
_TEST = {CHAT_KIND_ADMIN: MSG_ADMIN_CHAT_TEST, CHAT_KIND_GENERAL: MSG_GENERAL_CHAT_TEST}
_USAGE = {CHAT_KIND_ADMIN: MSG_ADMINS_USAGE, CHAT_KIND_GENERAL: MSG_GENERAL_USAGE}

_MENTION_TOKEN = re.compile(r"(?<!\w)@\S+")
_MARKUP_LINK = re.compile(r"\[[^\]]+\]\((?:max://|https?://)[^)]+\)", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_CHAT_LINE = re.compile(r"(?i)^/(admins|general)(?:@\S+)?(?:\s+(\S+))?$")


def normalize_chat_kind(chat_kind: str | None) -> str:
    kind = (chat_kind or CHAT_KIND_ADMIN).strip().lower()
    if kind not in _CHAT_KINDS:
        raise ValueError(f"Неизвестный вид чата: {chat_kind}")
    return kind


def chat_linked_message(chat_kind: str) -> str:
    return _LINKED[normalize_chat_kind(chat_kind)]


def chat_unlinked_message(chat_kind: str) -> str:
    return _UNLINKED[normalize_chat_kind(chat_kind)]


def chat_test_message(chat_kind: str) -> str:
    return _TEST[normalize_chat_kind(chat_kind)]


def chat_usage_message(chat_kind: str) -> str:
    return _USAGE[normalize_chat_kind(chat_kind)]


def hash_admin_link_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def normalize_admins_text(text: str | None) -> str:
    """Убрать упоминания и markup, чтобы команда находилась и с @бота."""
    raw = (text or "").replace("\u200b", " ").replace("\ufeff", " ")
    raw = _HTML_TAG.sub(" ", raw)
    raw = _MARKUP_LINK.sub(" ", raw)
    raw = _MENTION_TOKEN.sub(" ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def classify_chat_link_text(text: str | None) -> tuple[str | None, str | None, str]:
    """(chat_kind или None, код или None, 'code' | 'missing' | 'none')."""
    raw = (text or "").strip()
    if not raw or not re.search(r"(?i)/(admins|general)\b", raw):
        return None, None, "none"
    cleaned = normalize_admins_text(raw)
    match = _CHAT_LINE.match(cleaned)
    if match is None:
        kind = CHAT_KIND_GENERAL if re.search(r"(?i)/general\b", raw) else CHAT_KIND_ADMIN
        return kind, None, "missing"
    cmd = match.group(1).lower()
    kind = CHAT_KIND_GENERAL if cmd == "general" else CHAT_KIND_ADMIN
    code = (match.group(2) or "").strip()
    if not code or code.startswith("@"):
        return kind, None, "missing"
    return kind, code, "code"


def classify_admins_text(text: str | None) -> tuple[str | None, str]:
    """(код или None, 'code' | 'missing' | 'none') только для /admins."""
    kind, code, status = classify_chat_link_text(text)
    if kind != CHAT_KIND_ADMIN:
        return None, "none"
    return code, status


def classify_general_text(text: str | None) -> tuple[str | None, str]:
    """(код или None, 'code' | 'missing' | 'none') только для /general."""
    kind, code, status = classify_chat_link_text(text)
    if kind != CHAT_KIND_GENERAL:
        return None, "none"
    return code, status


def parse_admins_code(text: str | None) -> str | None:
    code, kind = classify_admins_text(text)
    if kind != "code":
        return None
    return code


def create_admin_chat_link_token(
    db: Session, *, created_by_user_id: int | None, chat_kind: str = CHAT_KIND_ADMIN
) -> str:
    kind = normalize_chat_kind(chat_kind)
    now = utcnow_naive()
    db.execute(
        update(AdminChatLinkToken)
        .where(
            AdminChatLinkToken.used_at.is_(None),
            AdminChatLinkToken.chat_kind == kind,
        )
        .values(used_at=now)
    )
    plain = secrets.token_urlsafe(24)
    db.add(
        AdminChatLinkToken(
            chat_kind=kind,
            token_hash=hash_admin_link_token(plain),
            created_by_user_id=created_by_user_id,
            created_at=now,
            expires_at=now + LINK_TTL,
            used_at=None,
        )
    )
    db.flush()
    return plain


def consume_admin_chat_link_token(
    db: Session, plain: str, *, chat_kind: str = CHAT_KIND_ADMIN
) -> bool:
    """Пометить код использованным. True если валиден и вид чата совпал."""
    kind = normalize_chat_kind(chat_kind)
    code = (plain or "").strip()
    if not code:
        return False
    now = utcnow_naive()
    row = db.scalar(
        select(AdminChatLinkToken).where(
            AdminChatLinkToken.token_hash == hash_admin_link_token(code),
            AdminChatLinkToken.used_at.is_(None),
        )
    )
    if row is None or (row.chat_kind or CHAT_KIND_ADMIN) != kind:
        return False
    if row.expires_at < now:
        row.used_at = now
        db.flush()
        return False
    row.used_at = now
    db.flush()
    return True


def get_admin_chat_target(
    db: Session, channel: str, *, chat_kind: str = CHAT_KIND_ADMIN
) -> AdminChatTarget | None:
    ch = (channel or "").strip().lower()
    kind = normalize_chat_kind(chat_kind)
    return db.scalar(
        select(AdminChatTarget)
        .where(AdminChatTarget.channel == ch, AdminChatTarget.chat_kind == kind)
        .limit(1)
    )


def list_admin_chat_targets(
    db: Session, *, chat_kind: str = CHAT_KIND_ADMIN
) -> dict[str, AdminChatTarget | None]:
    kind = normalize_chat_kind(chat_kind)
    rows = {
        r.channel: r
        for r in db.scalars(select(AdminChatTarget).where(AdminChatTarget.chat_kind == kind)).all()
    }
    return {
        NotificationChannel.VK.value: rows.get(NotificationChannel.VK.value),
        NotificationChannel.MAX.value: rows.get(NotificationChannel.MAX.value),
        NotificationChannel.TELEGRAM.value: rows.get(NotificationChannel.TELEGRAM.value),
    }


def send_to_chat(channel: str, chat_id: int, text: str) -> None:
    ch = channel.strip().lower()
    if ch == NotificationChannel.VK.value:
        send_vk(text=text, peer_id=int(chat_id))
    elif ch == NotificationChannel.MAX.value:
        send_max(text=text, chat_id=int(chat_id))
    elif ch == NotificationChannel.TELEGRAM.value:
        send_telegram(int(chat_id), text)
    else:
        raise RuntimeError(f"Неизвестный канал чата: {channel}")


def bind_admin_chat(
    db: Session,
    *,
    channel: str,
    chat_id: int,
    title: str | None = None,
    chat_kind: str = CHAT_KIND_ADMIN,
) -> AdminChatTarget:
    """Привязать/заменить чат канала этого вида. Старому чату — уведомление об отключении."""
    ch = channel.strip().lower()
    kind = normalize_chat_kind(chat_kind)
    if ch not in (
        NotificationChannel.VK.value,
        NotificationChannel.MAX.value,
        NotificationChannel.TELEGRAM.value,
    ):
        raise ValueError(f"Неизвестный канал: {channel}")
    now = utcnow_naive()
    existing = get_admin_chat_target(db, ch, chat_kind=kind)
    if existing is not None and int(existing.chat_id) != int(chat_id):
        old_id = int(existing.chat_id)
        try:
            send_to_chat(ch, old_id, chat_unlinked_message(kind))
        except Exception:
            logger.exception("admin_chat: unlink notify failed channel=%s chat_id=%s", ch, old_id)
        existing.chat_id = int(chat_id)
        existing.title = (title or "").strip() or existing.title
        existing.linked_at = now
        db.flush()
        return existing
    if existing is not None:
        existing.title = (title or "").strip() or existing.title
        existing.linked_at = now
        db.flush()
        return existing
    row = AdminChatTarget(
        chat_kind=kind,
        channel=ch,
        chat_id=int(chat_id),
        title=(title or "").strip() or None,
        linked_at=now,
    )
    db.add(row)
    db.flush()
    return row


def unlink_admin_chat(
    db: Session, channel: str, *, notify: bool = True, chat_kind: str = CHAT_KIND_ADMIN
) -> None:
    ch = channel.strip().lower()
    kind = normalize_chat_kind(chat_kind)
    row = get_admin_chat_target(db, ch, chat_kind=kind)
    if row is None:
        return
    chat_id = int(row.chat_id)
    db.delete(row)
    db.flush()
    if notify:
        try:
            send_to_chat(ch, chat_id, chat_unlinked_message(kind))
        except Exception:
            logger.exception("admin_chat: unlink notify failed channel=%s", ch)


def try_bind_admin_chat_from_text(
    db: Session,
    *,
    channel: str,
    chat_id: int,
    text: str | None,
    title: str | None = None,
) -> str | None:
    """Если текст — /admins или /general, привязать этот вид. Без кода — подсказка. Иначе None."""
    kind, code, status = classify_chat_link_text(text)
    if status == "none" or kind is None:
        return None
    if status == "missing" or not code:
        return chat_usage_message(kind)
    if not consume_admin_chat_link_token(db, code, chat_kind=kind):
        db.commit()
        return MSG_ADMIN_CHAT_INVALID
    bind_admin_chat(db, channel=channel, chat_id=int(chat_id), title=title, chat_kind=kind)
    db.commit()
    return chat_linked_message(kind)


def admin_chats_for_ui(db: Session, *, chat_kind: str = CHAT_KIND_ADMIN) -> list[dict[str, Any]]:
    targets = list_admin_chat_targets(db, chat_kind=chat_kind)
    labels = {
        NotificationChannel.VK.value: "VK",
        NotificationChannel.MAX.value: "Max",
        NotificationChannel.TELEGRAM.value: "Telegram",
    }
    out = []
    for ch, label in labels.items():
        row = targets.get(ch)
        out.append(
            {
                "channel": ch,
                "label": label,
                "connected": row is not None,
                "chat_id": int(row.chat_id) if row is not None else None,
                "title": (row.title if row is not None else None) or "",
                "linked_at": row.linked_at if row is not None else None,
            }
        )
    return out
