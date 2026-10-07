"""Общий чат админов: привязка /admins <код>, хранение chat_id, тест/отключение."""

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
MSG_ADMIN_CHAT_LINKED = "Чат админов подключён"
MSG_ADMIN_CHAT_UNLINKED = "Чат админов отключён"
MSG_ADMIN_CHAT_INVALID = "Код устарел, запросите новый в CRM"
MSG_ADMIN_CHAT_TEST = "Тест: чат админов Живём Плетём"

_ADMINS_RE = re.compile(r"(?i)^\s*/admins(?:@\S+)?\s+(\S+)\s*$")


def hash_admin_link_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def parse_admins_code(text: str | None) -> str | None:
    t = (text or "").strip()
    if not t:
        return None
    m = _ADMINS_RE.match(t)
    if not m:
        return None
    return m.group(1).strip() or None


def create_admin_chat_link_token(db: Session, *, created_by_user_id: int | None) -> str:
    now = utcnow_naive()
    db.execute(
        update(AdminChatLinkToken)
        .where(AdminChatLinkToken.used_at.is_(None))
        .values(used_at=now)
    )
    plain = secrets.token_urlsafe(24)
    db.add(
        AdminChatLinkToken(
            token_hash=hash_admin_link_token(plain),
            created_by_user_id=created_by_user_id,
            created_at=now,
            expires_at=now + LINK_TTL,
            used_at=None,
        )
    )
    db.flush()
    return plain


def consume_admin_chat_link_token(db: Session, plain: str) -> bool:
    """Пометить код использованным. True если валиден."""
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
    if row is None:
        return False
    if row.expires_at < now:
        row.used_at = now
        db.flush()
        return False
    row.used_at = now
    db.flush()
    return True


def get_admin_chat_target(db: Session, channel: str) -> AdminChatTarget | None:
    ch = (channel or "").strip().lower()
    return db.scalar(select(AdminChatTarget).where(AdminChatTarget.channel == ch).limit(1))


def list_admin_chat_targets(db: Session) -> dict[str, AdminChatTarget | None]:
    rows = {r.channel: r for r in db.scalars(select(AdminChatTarget)).all()}
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
        raise RuntimeError(f"Неизвестный канал чата админов: {channel}")


def bind_admin_chat(
    db: Session,
    *,
    channel: str,
    chat_id: int,
    title: str | None = None,
) -> AdminChatTarget:
    """Привязать/заменить чат канала. Старому чату — уведомление об отключении."""
    ch = channel.strip().lower()
    if ch not in (
        NotificationChannel.VK.value,
        NotificationChannel.MAX.value,
        NotificationChannel.TELEGRAM.value,
    ):
        raise ValueError(f"Неизвестный канал: {channel}")
    now = utcnow_naive()
    existing = get_admin_chat_target(db, ch)
    if existing is not None and int(existing.chat_id) != int(chat_id):
        old_id = int(existing.chat_id)
        try:
            send_to_chat(ch, old_id, MSG_ADMIN_CHAT_UNLINKED)
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
        channel=ch,
        chat_id=int(chat_id),
        title=(title or "").strip() or None,
        linked_at=now,
    )
    db.add(row)
    db.flush()
    return row


def unlink_admin_chat(db: Session, channel: str, *, notify: bool = True) -> None:
    ch = channel.strip().lower()
    row = get_admin_chat_target(db, ch)
    if row is None:
        return
    chat_id = int(row.chat_id)
    db.delete(row)
    db.flush()
    if notify:
        try:
            send_to_chat(ch, chat_id, MSG_ADMIN_CHAT_UNLINKED)
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
    """Если текст — /admins <код>, привязать. Возвращает ответное сообщение или None."""
    code = parse_admins_code(text)
    if code is None:
        return None
    if not consume_admin_chat_link_token(db, code):
        db.commit()
        return MSG_ADMIN_CHAT_INVALID
    bind_admin_chat(db, channel=channel, chat_id=int(chat_id), title=title)
    db.commit()
    return MSG_ADMIN_CHAT_LINKED


def admin_chats_for_ui(db: Session) -> list[dict[str, Any]]:
    targets = list_admin_chat_targets(db)
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
