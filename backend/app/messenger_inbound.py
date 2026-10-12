"""Входящие сообщения клиентов: 1–5, выбор брони по последнему напоминанию."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_chat import list_admin_chat_targets
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.client_bot_replies import (
    BOT_REPLY_DEFAULTS,
    contact_reply_key,
    render_bot_reply,
    vk_community_dialog_url,
)
from app.client_notifications import _masters_csv, _services_block, get_services_marker
from app.consultation_booking import OPEN_BOOKING_STATUSES
from app.db.models import (
    Booking,
    BookingMaster,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    Client,
    ClientNotificationSend,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
)
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.notifications import booking_planned_master_user_ids, send_max, send_telegram, send_vk
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

EVENT_BOOKING_CLIENT_CANCEL_REQUEST = "booking_client_cancel_request"
EVENT_BOOKING_CLIENT_RESCHEDULE_REQUEST = "booking_client_reschedule_request"
EVENT_CLIENT_CONTACT_REQUEST = "client_contact_request"

MSG_HINT = BOT_REPLY_DEFAULTS["hint"]
MSG_NO_BOOKING = BOT_REPLY_DEFAULTS["no_booking"]
MSG_NO_BOOKING_RESCHEDULE = BOT_REPLY_DEFAULTS["no_booking_reschedule"]
MSG_ALREADY_CONFIRMED = "Запись уже подтверждена"
MSG_CONFIRMED = BOT_REPLY_DEFAULTS["confirm_1"]
MSG_CANCEL_REQUESTED = BOT_REPLY_DEFAULTS["cancel_3"]
MSG_RESCHEDULE_REQUESTED = BOT_REPLY_DEFAULTS["reschedule_2"]

_REPLY_RE = re.compile(r"^\s*([1-5])\s*[.!]?\s*$")

_CHANNEL_LABEL = {
    NotificationChannel.VK.value: "VK",
    NotificationChannel.MAX.value: "Max",
    NotificationChannel.TELEGRAM.value: "Telegram",
}


def normalize_client_reply(text: str | None) -> str | None:
    """Вернуть '1'..'5' или None."""
    m = _REPLY_RE.match(text or "")
    if not m:
        return None
    return m.group(1)


def find_user_by_channel_id(db: Session, channel: str, messenger_id: int) -> User | None:
    ch = channel.strip().lower()
    if ch == NotificationChannel.TELEGRAM.value:
        return db.scalar(select(User).where(User.telegram_chat_id == int(messenger_id)).limit(1))
    if ch == NotificationChannel.VK.value:
        return db.scalar(select(User).where(User.vk_user_id == int(messenger_id)).limit(1))
    if ch == NotificationChannel.MAX.value:
        return db.scalar(select(User).where(User.max_user_id == int(messenger_id)).limit(1))
    return None


def find_client_by_channel_id(db: Session, channel: str, messenger_id: int) -> Client | None:
    ch = channel.strip().lower()
    if ch == NotificationChannel.TELEGRAM.value:
        return db.scalar(select(Client).where(Client.telegram_chat_id == int(messenger_id)).limit(1))
    if ch == NotificationChannel.VK.value:
        return db.scalar(select(Client).where(Client.vk_user_id == int(messenger_id)).limit(1))
    if ch == NotificationChannel.MAX.value:
        return db.scalar(select(Client).where(Client.max_user_id == int(messenger_id)).limit(1))
    return None


def reply_to_messenger(channel: str, messenger_id: int, text: str) -> None:
    ch = channel.strip().lower()
    if ch == NotificationChannel.TELEGRAM.value:
        send_telegram(int(messenger_id), text)
    elif ch == NotificationChannel.VK.value:
        send_vk(int(messenger_id), text)
    elif ch == NotificationChannel.MAX.value:
        send_max(int(messenger_id), text)
    else:
        raise RuntimeError(f"Неизвестный канал: {channel}")


def find_booking_for_client_reply(db: Session, client: Client) -> Booking | None:
    """Бронь по последнему client_booking_reminder, начало ещё в будущем."""
    now = utcnow_naive()
    send = db.scalar(
        select(ClientNotificationSend)
        .where(
            ClientNotificationSend.client_id == int(client.id),
            ClientNotificationSend.event_type == EVENT_CLIENT_BOOKING_REMINDER,
        )
        .order_by(ClientNotificationSend.sent_at.desc(), ClientNotificationSend.id.desc())
        .limit(1)
    )
    if send is None:
        return None
    booking = db.get(Booking, int(send.booking_id))
    if booking is None:
        return None
    if booking.client_id != int(client.id):
        return None
    if booking.status not in OPEN_BOOKING_STATUSES:
        return None
    if booking.planned_date is None or booking.planned_date <= now:
        return None
    return booking


def _channel_label(channel: str) -> str:
    return _CHANNEL_LABEL.get(channel.strip().lower(), channel)


def _badge_text(prefix: str, when_dt: datetime | None, via_raw: str | None, tz_name: str) -> str:
    when = format_naive_utc_datetime(when_dt, tz_name, "%d.%m %H:%M") if when_dt else ""
    via = _channel_label(via_raw or "")
    if via and when:
        return f"{prefix} ({via}, {when})"
    if when:
        return f"{prefix} ({when})"
    return prefix


def client_reply_badges(booking: Booking, *, tz_name: str) -> list[dict[str, str]]:
    """Бейджи подтверждения, отмены и переноса. kind пустой у подтверждения — кнопку не рисуем."""
    badges: list[dict[str, str]] = []
    if booking.client_confirmed_at is not None:
        badges.append(
            {
                "kind": "",
                "tone": "ok",
                "text": _badge_text(
                    "Клиент подтвердил",
                    booking.client_confirmed_at,
                    booking.client_confirmed_via,
                    tz_name,
                ),
            }
        )
    if booking.client_cancel_requested_at is not None:
        badges.append(
            {
                "kind": "cancel",
                "tone": "danger",
                "text": _badge_text(
                    "Клиент просит отменить",
                    booking.client_cancel_requested_at,
                    booking.client_cancel_requested_via,
                    tz_name,
                ),
            }
        )
    if booking.client_reschedule_requested_at is not None:
        badges.append(
            {
                "kind": "reschedule",
                "tone": "warn",
                "text": _badge_text(
                    "Клиент просит перенести",
                    booking.client_reschedule_requested_at,
                    booking.client_reschedule_requested_via,
                    tz_name,
                ),
            }
        )
    return badges


def client_confirm_badge(booking: Booking, *, tz_name: str) -> str | None:
    badges = client_reply_badges(booking, tz_name=tz_name)
    if not badges:
        return None
    # Совместимость: сначала запрос отмены, иначе первый бейдж.
    for item in badges:
        if item["kind"] == "cancel":
            return item["text"]
    return badges[0]["text"]


def client_confirm_icon(booking: Booking) -> str:
    if booking.client_cancel_requested_at is not None:
        return "❗"
    if booking.client_reschedule_requested_at is not None:
        return "🔀"
    if booking.client_confirmed_at is not None:
        return "✅"
    return ""


def mark_client_reply_handled(db: Session, booking: Booking, kind: str, *, user_id: int) -> None:
    """Снять отметку запроса. kind: cancel | reschedule."""
    now = utcnow_naive()
    if kind == "cancel":
        if booking.client_cancel_requested_at is None:
            raise ValueError("Запроса на отмену нет.")
        booking.client_cancel_requested_at = None
        booking.client_cancel_requested_via = None
        booking.client_cancel_handled_at = now
        booking.client_cancel_handled_by_user_id = int(user_id)
    elif kind == "reschedule":
        if booking.client_reschedule_requested_at is None:
            raise ValueError("Запроса на перенос нет.")
        booking.client_reschedule_requested_at = None
        booking.client_reschedule_requested_via = None
        booking.client_reschedule_handled_at = now
        booking.client_reschedule_handled_by_user_id = int(user_id)
    else:
        raise ValueError("Неизвестный запрос.")
    db.flush()


def _build_cancel_request_text(db: Session, booking: Booking, client: Client, channel: str) -> str:
    tz = get_display_timezone(db)
    marker = get_services_marker(db)
    when = format_naive_utc_datetime(booking.planned_date, tz) or "—"
    services = _services_block(booking, marker) or "—"
    masters = _masters_csv(booking) or "—"
    via = _channel_label(channel)
    return (
        f"Клиент просит отменить запись\n"
        f"Клиент: {client.name}\n"
        f"Когда: {when}\n"
        f"Услуги:\n{services}\n"
        f"Мастера: {masters}\n"
        f"Канал ответа: {via}\n"
        f"Бронь: /bookings/{int(booking.id)}"
    )


def _enqueue_cancel_request_notices(
    db: Session,
    booking: Booking,
    client: Client,
    *,
    channel: str,
) -> None:
    text = _build_cancel_request_text(db, booking, client, channel)
    # мастерам брони
    from app.booking_reminders import _user_channel_targets

    master_ids = booking_planned_master_user_ids(booking)
    for uid in master_ids:
        user = db.get(User, uid)
        if user is None:
            continue
        for ch, target_id in _user_channel_targets(user):
            key = f"bccr:{int(booking.id)}:{uid}:{ch.value}"
            if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)):
                continue
            payload = {
                "text": text,
                "target_id": target_id,
                "target_kind": "user",
                "event_type": EVENT_BOOKING_CLIENT_CANCEL_REQUEST,
                "booking_id": int(booking.id),
            }
            db.add(
                NotificationOutbox(
                    user_id=uid,
                    client_id=None,
                    target_kind="user",
                    booking_id=int(booking.id),
                    channel=ch,
                    event_type=EVENT_BOOKING_CLIENT_CANCEL_REQUEST,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    status=NotificationOutboxStatus.PENDING,
                    dedupe_key=key,
                    attempt_count=0,
                    created_at=utcnow_naive(),
                )
            )
    # чаты админов
    for ch, row in list_admin_chat_targets(db).items():
        if row is None:
            continue
        key = f"bccr_admin:{int(booking.id)}:{ch}"
        if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)):
            continue
        channel_enum = NotificationChannel(ch)
        payload = {
            "text": text,
            "target_id": int(row.chat_id),
            "target_kind": "admin_chat",
            "event_type": EVENT_BOOKING_CLIENT_CANCEL_REQUEST,
            "booking_id": int(booking.id),
        }
        db.add(
            NotificationOutbox(
                user_id=None,
                client_id=None,
                target_kind="admin_chat",
                booking_id=int(booking.id),
                channel=channel_enum,
                event_type=EVENT_BOOKING_CLIENT_CANCEL_REQUEST,
                payload_json=json.dumps(payload, ensure_ascii=False),
                status=NotificationOutboxStatus.PENDING,
                dedupe_key=key,
                attempt_count=0,
                created_at=utcnow_naive(),
            )
        )
    db.flush()


def _booking_facts(db: Session, booking: Booking, client: Client) -> str:
    tz = get_display_timezone(db)
    marker = get_services_marker(db)
    when = format_naive_utc_datetime(booking.planned_date, tz) or "—"
    services = _services_block(booking, marker) or "—"
    masters = _masters_csv(booking) or "—"
    return (
        f"Клиент: {client.name}\n"
        f"Когда: {when}\n"
        f"Услуги:\n{services}\n"
        f"Мастера: {masters}\n"
        f"Бронь: /bookings/{int(booking.id)}"
    )


def _load_booking_graph(db: Session, booking: Booking) -> Booking:
    loaded = db.scalar(
        select(Booking)
        .where(Booking.id == int(booking.id))
        .options(
            selectinload(Booking.masters).selectinload(BookingMaster.master),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.masters)
            .selectinload(BookingPlannedServiceMaster.master),
        )
        .limit(1)
    )
    return loaded or booking


def _enqueue_admin_chats(
    db: Session,
    *,
    text: str,
    event_type: str,
    dedupe_prefix: str,
    booking_id: int | None,
    client_id: int | None,
) -> None:
    for ch, row in list_admin_chat_targets(db).items():
        if row is None:
            continue
        key = f"{dedupe_prefix}:{ch}"
        if len(key) > 200:
            key = key[:200]
        if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)):
            continue
        channel_enum = NotificationChannel(ch)
        payload = {
            "text": text,
            "target_id": int(row.chat_id),
            "target_kind": "admin_chat",
            "event_type": event_type,
            "booking_id": int(booking_id) if booking_id else None,
            "client_id": int(client_id) if client_id else None,
        }
        db.add(
            NotificationOutbox(
                user_id=None,
                client_id=int(client_id) if client_id else None,
                target_kind="admin_chat",
                booking_id=int(booking_id) if booking_id else None,
                channel=channel_enum,
                event_type=event_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
                status=NotificationOutboxStatus.PENDING,
                dedupe_key=key,
                attempt_count=0,
                created_at=utcnow_naive(),
            )
        )
    db.flush()


def _enqueue_reschedule_admin(db: Session, booking: Booking, client: Client, *, channel: str) -> None:
    via = _channel_label(channel)
    text = (
        "Клиентка просит перенести запись\n"
        f"{_booking_facts(db, booking, client)}\n"
        f"Канал ответа: {via}"
    )
    _enqueue_admin_chats(
        db,
        text=text,
        event_type=EVENT_BOOKING_CLIENT_RESCHEDULE_REQUEST,
        dedupe_prefix=(
            f"bcrs_admin:{int(booking.id)}:"
            f"{booking.client_reschedule_requested_at.strftime('%Y%m%d%H%M%S%f') if booking.client_reschedule_requested_at else '0'}"
        ),
        booking_id=int(booking.id),
        client_id=int(client.id),
    )


def _enqueue_contact_admin(db: Session, client: Client, *, channel: str, messenger_id: int) -> None:
    via = _channel_label(channel)
    lines = [
        "Клиентка просит связаться",
        f"Клиент: {client.name}",
        f"Канал: {via}",
        f"Карточка: /clients/{int(client.id)}",
    ]
    if channel.strip().lower() == NotificationChannel.VK.value and client.vk_user_id:
        dialog = vk_community_dialog_url(int(client.vk_user_id))
        if dialog:
            lines.append(f"Диалог VK: {dialog}")
    stamp = utcnow_naive().strftime("%Y%m%d%H%M%S%f")
    _enqueue_admin_chats(
        db,
        text="\n".join(lines),
        event_type=EVENT_CLIENT_CONTACT_REQUEST,
        dedupe_prefix=f"bccnt:{int(client.id)}:{int(messenger_id)}:{stamp}",
        booking_id=None,
        client_id=int(client.id),
    )


def handle_client_text(
    db: Session,
    *,
    channel: str,
    messenger_id: int,
    text: str | None,
) -> str | None:
    """Обработать текст от клиента. Возвращает ответ или None (если не клиент / пусто)."""
    if find_user_by_channel_id(db, channel, messenger_id) is not None:
        return None
    client = find_client_by_channel_id(db, channel, messenger_id)
    if client is None:
        return None
    reply = normalize_client_reply(text)
    ch = channel.strip().lower()
    if reply is None:
        return render_bot_reply(db, "hint")

    # 4 и 5 не требуют активной брони.
    if reply == "4":
        try:
            _enqueue_contact_admin(db, client, channel=ch, messenger_id=int(messenger_id))
        except Exception:
            logger.exception("messenger_inbound: contact notify enqueue failed client=%s", client.id)
        return render_bot_reply(db, contact_reply_key(ch))
    if reply == "5":
        return render_bot_reply(db, "directions_5")

    booking = find_booking_for_client_reply(db, client)
    if booking is None:
        if reply == "2":
            return render_bot_reply(db, "no_booking_reschedule")
        return render_bot_reply(db, "no_booking")

    booking = _load_booking_graph(db, booking)
    now = utcnow_naive()
    if reply == "1":
        if booking.client_confirmed_at is not None:
            return MSG_ALREADY_CONFIRMED
        booking.client_confirmed_at = now
        booking.client_confirmed_via = ch
        db.flush()
        return render_bot_reply(db, "confirm_1")
    if reply == "2":
        repeat = booking.client_reschedule_requested_at is not None
        booking.client_reschedule_requested_at = now
        booking.client_reschedule_requested_via = ch
        db.flush()
        if not repeat:
            try:
                _enqueue_reschedule_admin(db, booking, client, channel=ch)
            except Exception:
                logger.exception(
                    "messenger_inbound: reschedule notify enqueue failed booking=%s", booking.id
                )
        return render_bot_reply(db, "reschedule_2")

    # reply == "3" — логика отмены как раньше, текст из настройки.
    booking.client_cancel_requested_at = now
    booking.client_cancel_requested_via = ch
    db.flush()
    try:
        _enqueue_cancel_request_notices(db, booking, client, channel=ch)
    except Exception:
        logger.exception("messenger_inbound: cancel notify enqueue failed booking=%s", booking.id)
    return render_bot_reply(db, "cancel_3")


def process_inbound_message(
    db: Session,
    *,
    channel: str,
    messenger_id: int,
    text: str | None,
) -> None:
    """Обработать и ответить клиенту. Ошибки глотает."""
    try:
        answer = handle_client_text(db, channel=channel, messenger_id=messenger_id, text=text)
        db.commit()
        if answer:
            try:
                reply_to_messenger(channel, messenger_id, answer)
            except Exception:
                logger.exception(
                    "messenger_inbound: reply failed channel=%s id=%s", channel, messenger_id
                )
    except Exception:
        logger.exception("messenger_inbound: handler error channel=%s", channel)
        try:
            db.rollback()
        except Exception:
            pass
