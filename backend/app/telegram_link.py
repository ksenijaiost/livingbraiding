"""Привязка Telegram к сотруднику: одноразовые коды и deep link."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import TelegramLinkToken, User
from app.settings import get_settings
from app.time_utils import utcnow_naive

LINK_TOKEN_TTL = timedelta(hours=24)
MSG_LINKED_OK = "Готово, уведомления подключены"
MSG_LINK_INVALID = "Ссылка устарела, запросите новую в CRM"


def hash_telegram_link_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def telegram_deep_link(plain_token: str) -> str | None:
    """https://t.me/<BOT_USERNAME>?start=<token> или None, если username не задан."""
    username = (get_settings().telegram_bot_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start={plain_token}"


def create_telegram_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    """Создать одноразовый код. Возвращает (plain_token, deep_link|None).

    Старые неиспользованные коды этого пользователя помечаются использованными.
    """
    now = utcnow_naive()
    db.execute(
        update(TelegramLinkToken)
        .where(
            TelegramLinkToken.user_id == int(user_id),
            TelegramLinkToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    plain = secrets.token_urlsafe(24)
    row = TelegramLinkToken(
        user_id=int(user_id),
        token_hash=hash_telegram_link_token(plain),
        created_at=now,
        expires_at=now + LINK_TOKEN_TTL,
        used_at=None,
    )
    db.add(row)
    db.flush()
    return plain, telegram_deep_link(plain)


def consume_telegram_link_token(db: Session, plain_token: str) -> User | None:
    """Найти валидный код, пометить использованным, вернуть User; иначе None."""
    plain = (plain_token or "").strip()
    if not plain:
        return None
    now = utcnow_naive()
    row = db.scalar(
        select(TelegramLinkToken).where(
            TelegramLinkToken.token_hash == hash_telegram_link_token(plain),
            TelegramLinkToken.used_at.is_(None),
        )
    )
    if row is None:
        return None
    if row.expires_at < now:
        row.used_at = now
        db.flush()
        return None
    user = db.get(User, int(row.user_id))
    if user is None:
        row.used_at = now
        db.flush()
        return None
    row.used_at = now
    db.flush()
    return user


def bind_telegram_chat(db: Session, user: User, chat_id: int) -> None:
    """Привязать chat_id к сотруднику; снять тот же chat_id с других пользователей."""
    cid = int(chat_id)
    others = list(
        db.scalars(select(User).where(User.telegram_chat_id == cid, User.id != int(user.id))).all()
    )
    for o in others:
        o.telegram_chat_id = None
    user.telegram_chat_id = cid
    db.flush()


def unlink_telegram_chat(db: Session, user: User) -> None:
    user.telegram_chat_id = None
    db.flush()


def parse_telegram_start_code(text: str | None) -> str | None:
    """Извлечь код из «/start <код>» или «/start@bot <код>». Bare /start → None."""
    t = (text or "").strip()
    if not t.startswith("/start"):
        return None
    parts = t.split(maxsplit=1)
    if len(parts) < 2:
        return None
    code = parts[1].strip().split()[0].strip()
    return code or None
