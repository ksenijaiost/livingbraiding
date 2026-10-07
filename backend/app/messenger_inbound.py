"""Входящие сообщения клиентов: 1 / 3, выбор брони по последнему напоминанию."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_chat import list_admin_chat_targets
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
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

MSG_HINT = "Ответьте 1 — подтвердить, 3 — отменить запись"
MSG_NO_BOOKING = "Не нашли активную запись, свяжитесь с нами"
MSG_ALREADY_CONFIRMED = "Запись уже подтверждена"
MSG_CONFIRMED = "Спасибо, запись подтверждена"
MSG_CANCEL_REQUESTED = "Передали администратору, с вами свяжутся"

_REPLY_RE = re.compile(r"^\s*([13])\s*[.!]?\s*$")

_CHANNEL_LABEL = {
    NotificationChannel.VK.value: "VK",
    NotificationChannel.MAX.value: "Max",
    NotificationChannel.TELEGRAM.value: "Telegram",
}


def normalize_client_reply(text: str | None) -> str | None:
    """Вернуть '1' / '3' или None."""
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


def client_confirm_badge(booking: Booking, *, tz_name: str) -> str | None:
    if booking.client_cancel_requested_at is not None:
        when = format_naive_utc_datetime(
            booking.client_cancel_requested_at, tz_name, "%d.%m %H:%M"
        )
        via = _channel_label(booking.client_cancel_requested_via or "")
        return f"Клиент просит отменить ({via}, {when})" if via else f"Клиент просит отменить ({when})"
    if booking.client_confirmed_at is not None:
        when = format_naive_utc_datetime(booking.client_confirmed_at, tz_name, "%d.%m %H:%M")
        via = _channel_label(booking.client_confirmed_via or "")
        return f"Клиент подтвердил ({via}, {when})" if via else f"Клиент подтвердил ({when})"
    return None


def client_confirm_icon(booking: Booking) -> str:
    if booking.client_cancel_requested_at is not None:
        return "❗"
    if booking.client_confirmed_at is not None:
        return "✅"
    return ""


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
    if reply is None:
        return MSG_HINT

    booking = find_booking_for_client_reply(db, client)
    if booking is None:
        return MSG_NO_BOOKING

    # подгрузить связи для текста отмены
    booking = db.scalar(
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
    ) or booking

    now = utcnow_naive()
    ch = channel.strip().lower()
    if reply == "1":
        if booking.client_confirmed_at is not None:
            return MSG_ALREADY_CONFIRMED
        booking.client_confirmed_at = now
        booking.client_confirmed_via = ch
        db.flush()
        return MSG_CONFIRMED

    # reply == "3"
    booking.client_cancel_requested_at = now
    booking.client_cancel_requested_via = ch
    db.flush()
    try:
        _enqueue_cancel_request_notices(db, booking, client, channel=ch)
    except Exception:
        logger.exception("messenger_inbound: cancel notify enqueue failed booking=%s", booking.id)
    return MSG_CANCEL_REQUESTED


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
