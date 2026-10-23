"""Одноразовые коды привязки каналов уведомлений (Telegram / VK / Max)."""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.client_phone import find_client_by_normalized_phone
from app.db.models import Client, NotificationChannel, TelegramLinkToken, User
from app.phone_ru import InvalidPhoneError, normalize_phone
from app.settings import get_settings
from app.time_utils import utcnow_naive

LINK_TOKEN_TTL = timedelta(hours=24)
MSG_LINKED_OK = "Готово, уведомления подключены"
MSG_LINK_INVALID = "Ссылка устарела, запросите новую в CRM"
MSG_MAX_TAKEN = "Этот аккаунт Max уже привязан к другой сотруднице"
MSG_CLIENT_MAX_TAKEN = "Этот аккаунт Max уже привязан к другому клиенту"
MSG_CLIENT_LINKED_OK = "Готово, уведомления о записях подключены"
MSG_CLIENT_PHONE_NOT_FOUND = (
    "Не нашли клиента с таким номером телефона. Проверьте номер или обратитесь к администратору."
)
MSG_ASK_CLIENT_PHONE = (
    "Здравствуйте! Чтобы подключить уведомления о записях, "
    "отправьте в ответ свой номер телефона (например, 89001234567)."
)
MSG_CLIENT_ALREADY_LINKED = "Уведомления о записях уже подключены."

# Без похожих символов (0/o, 1/l/i). Всегда строчные, с хотя бы одной буквой.
_LINK_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_LINK_CODE_LETTERS = "abcdefghjkmnpqrstuvwxyz"
_BARE_LINK_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_BIND_TEXT_PREFIXES = (
    "подключить уведомления",
    "добрый день",
    "/start",
    "привязка",
    "уведомления",
    "подключить",
    "здравствуйте",
    "привет",
    "начать",
)
_BIND_GREETINGS = {
    "/start",
    "начать",
    "привязка",
    "уведомления",
    "подключить",
    "подключить уведомления",
    "привет",
    "здравствуйте",
}

CHANNEL_TELEGRAM = NotificationChannel.TELEGRAM.value
CHANNEL_VK = NotificationChannel.VK.value
CHANNEL_MAX = NotificationChannel.MAX.value

_VK_PRIVYAZKA_RE = re.compile(r"(?i)^\s*привязка\s+(\S+)\s*$")
_MAX_PRIVYAZKA_RE = re.compile(r"(?i)^\s*привязка\s+(\S+)\s*$")


def hash_link_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def generate_link_code() -> str:
    """6 символов, всегда есть буква, чтобы код не совпал с цифрой ответа или телефоном."""
    while True:
        chars = [secrets.choice(_LINK_CODE_ALPHABET) for _ in range(6)]
        if any(ch in _LINK_CODE_LETTERS for ch in chars):
            return "".join(chars)


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
    plain = generate_link_code()
    row = TelegramLinkToken(
        user_id=int(user_id),
        client_id=None,
        token_hash=hash_link_token(plain),
        channel=ch,
        created_at=now,
        expires_at=now + LINK_TOKEN_TTL,
        used_at=None,
    )
    db.add(row)
    db.flush()
    return plain, _deep_link_for_channel(ch, plain)


def _deep_link_for_channel(ch: str, plain: str) -> str | None:
    if ch == CHANNEL_TELEGRAM:
        return telegram_deep_link(plain)
    if ch == CHANNEL_VK:
        return vk_deep_link(plain)
    return max_deep_link(plain)


def create_telegram_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    return create_link_token(db, user_id, channel=CHANNEL_TELEGRAM)


def create_vk_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    return create_link_token(db, user_id, channel=CHANNEL_VK)


def create_max_link_token(db: Session, user_id: int) -> tuple[str, str | None]:
    return create_link_token(db, user_id, channel=CHANNEL_MAX)


def create_client_link_token(db: Session, client_id: int, *, channel: str) -> tuple[str, str | None]:
    """Одноразовый код привязки канала для клиента."""
    ch = (channel or "").strip().lower()
    if ch not in (CHANNEL_TELEGRAM, CHANNEL_VK, CHANNEL_MAX):
        raise ValueError(f"Неизвестный канал привязки: {channel}")
    now = utcnow_naive()
    db.execute(
        update(TelegramLinkToken)
        .where(
            TelegramLinkToken.client_id == int(client_id),
            TelegramLinkToken.channel == ch,
            TelegramLinkToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    plain = generate_link_code()
    db.add(
        TelegramLinkToken(
            user_id=None,
            client_id=int(client_id),
            token_hash=hash_link_token(plain),
            channel=ch,
            created_at=now,
            expires_at=now + LINK_TOKEN_TTL,
            used_at=None,
        )
    )
    db.flush()
    return plain, _deep_link_for_channel(ch, plain)


def _find_unused_link_row(
    db: Session,
    plain: str,
    *,
    channel: str | None,
) -> TelegramLinkToken | None:
    stmt = select(TelegramLinkToken).where(
        TelegramLinkToken.token_hash == hash_link_token(plain),
        TelegramLinkToken.used_at.is_(None),
    )
    if channel:
        stmt = stmt.where(TelegramLinkToken.channel == channel.strip().lower())
    return db.scalar(stmt)


def _load_valid_link_row(
    db: Session,
    plain_token: str,
    *,
    channel: str | None = None,
) -> TelegramLinkToken | None:
    plain = (plain_token or "").strip()
    if not plain:
        return None
    now = utcnow_naive()
    row = _find_unused_link_row(db, plain, channel=channel)
    if row is None:
        lowered = plain.lower()
        if lowered != plain:
            row = _find_unused_link_row(db, lowered, channel=channel)
    if row is None:
        return None
    if row.expires_at < now:
        row.used_at = now
        db.flush()
        return None
    return row


def consume_link_token(
    db: Session,
    plain_token: str,
    *,
    channel: str | None = None,
) -> User | None:
    """Найти валидный код сотрудника, пометить использованным, вернуть User; иначе None."""
    row = _load_valid_link_row(db, plain_token, channel=channel)
    if row is None or row.user_id is None:
        if row is not None and row.user_id is None:
            # клиентский код — не потребляем здесь
            return None
        return None
    user = db.get(User, int(row.user_id))
    if user is None:
        row.used_at = utcnow_naive()
        db.flush()
        return None
    row.used_at = utcnow_naive()
    db.flush()
    return user


def consume_client_link_token(
    db: Session,
    plain_token: str,
    *,
    channel: str | None = None,
) -> Client | None:
    """Найти валидный код клиента, пометить использованным."""
    row = _load_valid_link_row(db, plain_token, channel=channel)
    if row is None or row.client_id is None:
        return None
    client = db.get(Client, int(row.client_id))
    if client is None:
        row.used_at = utcnow_naive()
        db.flush()
        return None
    row.used_at = utcnow_naive()
    db.flush()
    return client


def consume_any_link_token(
    db: Session,
    plain_token: str,
    *,
    channel: str | None = None,
) -> tuple[User | None, Client | None]:
    """Потребить код: вернуть (user, None) или (None, client)."""
    row = _load_valid_link_row(db, plain_token, channel=channel)
    if row is None:
        return None, None
    now = utcnow_naive()
    if row.user_id is not None:
        user = db.get(User, int(row.user_id))
        row.used_at = now
        db.flush()
        return user, None
    if row.client_id is not None:
        client = db.get(Client, int(row.client_id))
        row.used_at = now
        db.flush()
        return None, client
    row.used_at = now
    db.flush()
    return None, None


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
    """max_user_id уже привязан к другой сотруднице."""


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


class ClientChannelTakenError(ValueError):
    """ID канала уже привязан к другому клиенту."""


def bind_client_telegram(db: Session, client: Client, chat_id: int) -> None:
    cid = int(chat_id)
    other = db.scalar(
        select(Client).where(Client.telegram_chat_id == cid, Client.id != int(client.id)).limit(1)
    )
    if other is not None:
        raise ClientChannelTakenError("Этот Telegram уже привязан к другому клиенту")
    client.telegram_chat_id = cid
    db.flush()


def bind_client_vk(db: Session, client: Client, vk_user_id: int) -> None:
    vid = int(vk_user_id)
    other = db.scalar(
        select(Client).where(Client.vk_user_id == vid, Client.id != int(client.id)).limit(1)
    )
    if other is not None:
        raise ClientChannelTakenError("Этот VK уже привязан к другому клиенту")
    client.vk_user_id = vid
    db.flush()


def bind_client_max(db: Session, client: Client, max_user_id: int) -> None:
    mid = int(max_user_id)
    other_c = db.scalar(
        select(Client).where(Client.max_user_id == mid, Client.id != int(client.id)).limit(1)
    )
    if other_c is not None:
        raise ClientChannelTakenError(MSG_CLIENT_MAX_TAKEN)
    client.max_user_id = mid
    db.flush()


def _bind_phrase(text: str | None) -> str:
    raw = (text or "").strip().lower().replace("ё", "е")
    raw = re.sub(r"[^\w\s/]+", " ", raw, flags=re.UNICODE)
    return re.sub(r"\s+", " ", raw).strip()


def is_client_bind_greeting_or_command(text: str | None) -> bool:
    """Команда или приветствие без телефона и без кода: /start, «Привет», «Привязка» и т.п."""
    return _bind_phrase(text) in _BIND_GREETINGS


def is_explicit_client_bind_word(text: str | None) -> bool:
    """«привязка» или «уведомления» без номера и без кода."""
    return _bind_phrase(text) in {"привязка", "уведомления"}


def extract_phone_for_client_bind(text: str | None) -> str | None:
    """Номер из сообщения, если после приветствия/команды остались только цифры телефона."""
    raw = (text or "").strip()
    if not raw:
        return None
    lowered = raw.lower().replace("ё", "е")
    rest = raw
    for prefix in _BIND_TEXT_PREFIXES:
        if lowered.startswith(prefix):
            rest = raw[len(prefix) :]
            rest = re.sub(r"^[\s,;:!?.\-—]+", "", rest)
            break
    rest = rest.strip()
    if not rest or re.search(r"[^\d\s+\-()]", rest):
        return None
    try:
        normalized = normalize_phone(rest)
    except InvalidPhoneError:
        return None
    if normalized and len(normalized) == 11 and normalized.startswith("7"):
        return normalized
    return None


def try_bind_client_by_phone(
    db: Session,
    *,
    channel: str,
    messenger_id: int,
    phone_normalized: str,
) -> str:
    """Привязать канал клиента по номеру. Возвращает текст ответа бота."""
    client = find_client_by_normalized_phone(db, phone_normalized)
    if client is None:
        return MSG_CLIENT_PHONE_NOT_FOUND
    ch = (channel or "").strip().lower()
    try:
        if ch == CHANNEL_VK:
            bind_client_vk(db, client, int(messenger_id))
        elif ch == CHANNEL_MAX:
            bind_client_max(db, client, int(messenger_id))
        elif ch == CHANNEL_TELEGRAM:
            bind_client_telegram(db, client, int(messenger_id))
        else:
            raise ValueError(f"Неизвестный канал привязки: {channel}")
        db.commit()
    except ClientChannelTakenError as exc:
        db.rollback()
        return str(exc)
    return MSG_CLIENT_LINKED_OK


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
    if not t or extract_phone_for_client_bind(t):
        return None
    m = _VK_PRIVYAZKA_RE.match(t)
    if m:
        arg = m.group(1).strip()
        if arg and extract_phone_for_client_bind(arg) is None:
            return arg
        return None
    return _bare_link_code(t)


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
    if extract_phone_for_client_bind(t):
        return None
    m = _MAX_PRIVYAZKA_RE.match(t)
    if m:
        arg = m.group(1).strip()
        if arg and extract_phone_for_client_bind(arg) is None:
            return arg
        return None
    return _bare_link_code(t)


def _bare_link_code(text: str) -> str | None:
    """Голый код 6–64 символа: латиница, цифры, _ и -, с хотя бы одной буквой, не телефон."""
    if " " in text or "\n" in text:
        return None
    if _BARE_LINK_CODE_RE.fullmatch(text) is None:
        return None
    if not re.search(r"[A-Za-z]", text):
        return None
    if extract_phone_for_client_bind(text):
        return None
    return text
