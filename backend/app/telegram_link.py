"""Одноразовые коды привязки каналов уведомлений (Telegram / VK / Max)."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import NotificationChannel, TelegramLinkToken, User
from app.settings import get_settings
from app.time_utils import utcnow_naive

LINK_TOKEN_TTL = timedelta(hours=24)
MSG_LINKED_OK = "Готово, уведомления подключены"
MSG_LINK_INVALID = "Ссылка устарела, запросите новую в CRM"
MSG_MAX_TAKEN = "Этот аккаунт Max уже привязан к другому сотруднику"

CHANNEL_TELEGRAM = NotificationChannel.TELEGRAM.value
CHANNEL_VK = NotificationChannel.VK.value
CHANNEL_MAX = NotificationChannel.MAX.value

_VK_PRIVYAZKA_RE = re.compile(r"(?i)^\s*привязка\s+(\S+)\s*$")
_MAX_PRIVYAZKA_RE = re.compile(r"(?i)^\s*привязка\s+(\S+)\s*$")


def hash_link_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


# Обратная совместимость
hash_telegram_link_token = hash_link_token


def telegram_deep_link(plain_token: str) -> str | None:
    """https://t.me/<BOT_USERNAME>?start=<token> или None, если username не задан."""
    username = (get_settings().telegram_bot_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://t.me/{username}?start={plain_token}"


def vk_deep_link(plain_token: str) -> str | None:
    """https://vk.me/<VK_GROUP_DOMAIN>?ref=<token> или None."""
    domain = (get_settings().vk_group_domain or "").strip().lstrip("@").strip("/")
    if not domain:
        return None
    return f"https://vk.me/{domain}?ref={plain_token}"


def max_deep_link(plain_token: str) -> str | None:
    """https://max.ru/<MAX_BOT_USERNAME>?start=<token> или None."""
    username = (get_settings().max_bot_username or "").strip().lstrip("@")
    if not username:
        return None
    return f"https://max.ru/{username}?start={plain_token}"


def create_link_token(
    db: Session,
    user_id: int,
    *,
    channel: str,
) -> tuple[str, str | None]:
    """Создать одноразовый код для канала. Возвращает (plain_token, deep_link|None).

    Старые неиспользованные коды этого пользователя в том же канале помечаются использованными.
    """
    ch = (channel or "").strip().lower()
    if ch not in (CHANNEL_TELEGRAM, CHANNEL_VK, CHANNEL_MAX):
        raise ValueError(f"Неизвестный канал привязки: {channel}")
    now = utcnow_naive()
    db.execute(
        update(TelegramLinkToken)
        .where(
            TelegramLinkToken.user_id == int(user_id),
            TelegramLinkToken.channel == ch,
            TelegramLinkToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    plain = secrets.token_urlsafe(24)
    row = TelegramLinkToken(
        user_id=int(user_id),
        token_hash=hash_link_token(plain),
        channel=ch,
        created_at=now,
        expires_at=now + LINK_TOKEN_TTL,
        used_at=None,
    )
    db.add(row)
    db.flush()
    if ch == CHANNEL_TELEGRAM:
        deep = telegram_deep_link(plain)
    elif ch == CHANNEL_VK:
        deep = vk_deep_link(plain)
    else:
        deep = max_deep_link(plain)
    return plain, deep


def create_telegram_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    return create_link_token(db, user_id, channel=CHANNEL_TELEGRAM)


def create_vk_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    return create_link_token(db, user_id, channel=CHANNEL_VK)


def create_max_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    return create_link_token(db, user_id, channel=CHANNEL_MAX)


def consume_link_token(
    db: Session,
    plain_token: str,
    *,
    channel: str | None = None,
) -> User | None:
    """Найти валидный код, пометить использованным, вернуть User; иначе None."""
    plain = (plain_token or "").strip()
    if not plain:
        return None
    now = utcnow_naive()
    stmt = select(TelegramLinkToken).where(
        TelegramLinkToken.token_hash == hash_link_token(plain),
        TelegramLinkToken.used_at.is_(None),
    )
    if channel:
        stmt = stmt.where(TelegramLinkToken.channel == channel.strip().lower())
    row = db.scalar(stmt)
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


def consume_telegram_link_token(db: Session, plain_token: str) -> User | None:
    return consume_link_token(db, plain_token, channel=CHANNEL_TELEGRAM)


def consume_vk_link_token(db: Session, plain_token: str) -> User | None:
    return consume_link_token(db, plain_token, channel=CHANNEL_VK)


def consume_max_link_token(db: Session, plain_token: str) -> User | None:
    return consume_link_token(db, plain_token, channel=CHANNEL_MAX)


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


def bind_vk_user(db: Session, user: User, vk_user_id: int) -> None:
    vid = int(vk_user_id)
    others = list(
        db.scalars(select(User).where(User.vk_user_id == vid, User.id != int(user.id))).all()
    )
    for o in others:
        o.vk_user_id = None
    user.vk_user_id = vid
    db.flush()


def unlink_vk_user(db: Session, user: User) -> None:
    user.vk_user_id = None
    db.flush()


class MaxUserTakenError(ValueError):
    """max_user_id уже привязан к другому сотруднику."""


def bind_max_user(db: Session, user: User, max_user_id: int) -> None:
    """Привязать Max user_id; если занят другим — MaxUserTakenError."""
    mid = int(max_user_id)
    other = db.scalar(
        select(User).where(User.max_user_id == mid, User.id != int(user.id)).limit(1)
    )
    if other is not None:
        raise MaxUserTakenError(MSG_MAX_TAKEN)
    user.max_user_id = mid
    db.flush()


def unlink_max_user(db: Session, user: User) -> None:
    user.max_user_id = None
    db.flush()


# Обратная совместимость имён из промпта
clear_max_user = unlink_max_user


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


def parse_vk_link_code(
    text: str | None = None,
    *,
    ref: str | None = None,
    payload: str | None = None,
) -> str | None:
    """Код из ref/payload или текста «привязка <код>» / голый код."""
    for raw in (ref, payload):
        s = (raw or "").strip()
        if s:
            return s.split()[0].strip() or None
    t = (text or "").strip()
    if not t:
        return None
    m = _VK_PRIVYAZKA_RE.match(t)
    if m:
        return m.group(1).strip() or None
    # Голый код без пробелов (как token_urlsafe)
    if " " not in t and "\n" not in t and len(t) >= 16:
        return t
    return None


def parse_max_link_code(text: str | None = None, *, payload: str | None = None) -> str | None:
    """Код из payload bot_started или текста /start|/привязка."""
    s = (payload or "").strip()
    if s:
        return s.split()[0].strip() or None
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("/start"):
        return parse_telegram_start_code(t)
    m = _MAX_PRIVYAZKA_RE.match(t)
    if m:
        return m.group(1).strip() or None
    if " " not in t and "\n" not in t and len(t) >= 16:
        return t
    return None
