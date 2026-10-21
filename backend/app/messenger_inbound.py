"""Входящие сообщения клиентов: цифры из настроек, выбор брони по последнему напоминанию."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.admin_chat import list_admin_chat_targets
from app.booking_status_notify import EVENT_CLIENT_BOOKING_STATUS
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.client_bot_replies import (
    BOT_REPLY_DEFAULTS,
    contact_reply_key,
    render_bot_reply,
    vk_community_dialog_url,
)
from app.client_contact import (
    PENDING_KIND_CHOICE,
    PENDING_KIND_SELECTED,
    PENDING_TTL_MINUTES,
    contact_circle_client_ids,
)
from app.client_reply_digits import (
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_CONTACT,
    ACTION_DIRECTIONS,
    ACTION_RESCHEDULE,
    LEGACY_REPLY_HINT_LINE,
    action_for_digit,
    build_reply_hint,
    digit_for_action,
)
from app.client_notifications import _masters_csv, _services_block, get_services_marker
from app.consultation_booking import OPEN_BOOKING_STATUSES
from app.db.models import (
    Booking,
    BookingMaster,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    Client,
    ClientInboundMessage,
    ClientMessengerPending,
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
EVENT_CLIENT_BOT_REPLY = "client_bot_reply"

MSG_HINT = LEGACY_REPLY_HINT_LINE
MSG_NO_BOOKING = BOT_REPLY_DEFAULTS["no_booking"]
MSG_NO_BOOKING_RESCHEDULE = BOT_REPLY_DEFAULTS["no_booking_reschedule"]
MSG_ALREADY_CONFIRMED = "Запись уже подтверждена"
MSG_CONFIRMED = BOT_REPLY_DEFAULTS["confirm_1"]
MSG_CANCEL_REQUESTED = BOT_REPLY_DEFAULTS["cancel_3"]
MSG_RESCHEDULE_REQUESTED = BOT_REPLY_DEFAULTS["reschedule_2"]

_REPLY_RE = re.compile(r"^\s*([0-9])\s*[.!]?\s*$")

_CHANNEL_LABEL = {
    NotificationChannel.VK.value: "VK",
    NotificationChannel.MAX.value: "Max",
    NotificationChannel.TELEGRAM.value: "Telegram",
}


def _log_client_inbound(
    db: Session,
    *,
    client_id: int | None,
    booking_id: int | None,
    channel: str,
    messenger_id: int,
    text: str | None,
    parsed_action: str,
    answer: str | None,
    reply_key: str | None,
) -> str | None:
    raw = text if isinstance(text, str) else ""
    if len(raw) > 8000:
        raw = raw[:8000]
    db.add(
        ClientInboundMessage(
            client_id=int(client_id) if client_id is not None else None,
            booking_id=int(booking_id) if booking_id is not None else None,
            channel=channel.strip().lower(),
            external_user_id=int(messenger_id),
            text=raw,
            parsed_action=parsed_action or "unknown",
            received_at=utcnow_naive(),
        )
    )
    db.flush()
    if answer and client_id is not None and reply_key:
        db.info["lb_bot_reply"] = {
            "client_id": int(client_id),
            "booking_id": int(booking_id) if booking_id is not None else None,
            "reply_key": reply_key,
            "channel": channel.strip().lower(),
            "messenger_id": int(messenger_id),
            "text": answer,
        }
    else:
        db.info.pop("lb_bot_reply", None)
    return answer


def _save_bot_reply_outbox(
    db: Session,
    meta: dict,
    *,
    status: NotificationOutboxStatus,
    error: str | None,
    text: str,
) -> None:
    now = utcnow_naive()
    channel = str(meta.get("channel") or "")
    key = (
        f"cbr:{meta.get('client_id')}:{channel}:"
        f"{now.strftime('%Y%m%d%H%M%S%f')}:{meta.get('reply_key')}"
    )
    if len(key) > 200:
        key = key[:200]
    booking_id = meta.get("booking_id")
    client_id = meta.get("client_id")
    payload = {
        "text": text,
        "target_id": int(meta["messenger_id"]),
        "target_kind": "client",
        "event_type": EVENT_CLIENT_BOT_REPLY,
        "reply_key": meta.get("reply_key"),
        "booking_id": int(booking_id) if booking_id else None,
        "client_id": int(client_id) if client_id else None,
    }
    db.add(
        NotificationOutbox(
            user_id=None,
            client_id=int(client_id) if client_id else None,
            target_kind="client",
            booking_id=int(booking_id) if booking_id else None,
            channel=NotificationChannel(channel),
            event_type=EVENT_CLIENT_BOT_REPLY,
            payload_json=json.dumps(payload, ensure_ascii=False),
            status=status,
            error=(error or None),
            dedupe_key=key,
            attempt_count=1,
            created_at=now,
            scheduled_at=now,
            sent_at=now if status == NotificationOutboxStatus.SENT else None,
            body=text,
        )
    )
    db.flush()


def normalize_client_reply(text: str | None) -> str | None:
    """Вернуть одну цифру '0'..'9' или None."""
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


def _booking_still_suitable(booking: Booking | None, *, now: datetime, circle_ids: set[int]) -> bool:
    if booking is None:
        return False
    if int(booking.client_id) not in circle_ids:
        return False
    if booking.status not in OPEN_BOOKING_STATUSES:
        return False
    if booking.planned_date is None or booking.planned_date <= now:
        return False
    return True


def suitable_bookings_for_reply_circle(db: Session, sender: Client) -> list[Booking]:
    """Будущие OPEN-брони круга (отправитель + dependents) с reminder/status send."""
    circle_ids = set(contact_circle_client_ids(db, sender))
    now = utcnow_naive()
    sends = list(
        db.scalars(
            select(ClientNotificationSend)
            .where(
                ClientNotificationSend.client_id.in_(circle_ids),
                ClientNotificationSend.event_type.in_(
                    (EVENT_CLIENT_BOOKING_REMINDER, EVENT_CLIENT_BOOKING_STATUS)
                ),
            )
            .order_by(ClientNotificationSend.sent_at.desc(), ClientNotificationSend.id.desc())
        ).all()
    )
    out: list[Booking] = []
    seen: set[int] = set()
    for send in sends:
        bid = int(send.booking_id)
        if bid in seen:
            continue
        seen.add(bid)
        booking = db.get(Booking, bid)
        if _booking_still_suitable(booking, now=now, circle_ids=circle_ids):
            assert booking is not None
            out.append(booking)
    return out


def find_booking_for_client_reply(
    db: Session,
    client: Client,
    *,
    preferred_booking_id: int | None = None,
) -> Booking | None:
    """Бронь по последнему напоминанию/статусу в круге клиента (или preferred_booking_id)."""
    circle_ids = set(contact_circle_client_ids(db, client))
    now = utcnow_naive()
    if preferred_booking_id is not None:
        booking = db.get(Booking, int(preferred_booking_id))
        if _booking_still_suitable(booking, now=now, circle_ids=circle_ids):
            return booking
        return None
    suitable = suitable_bookings_for_reply_circle(db, client)
    return suitable[0] if suitable else None


def _reply_needs_booking_choice(bookings: list[Booking]) -> bool:
    clients = {int(b.client_id) for b in bookings}
    return len(clients) > 1


def _format_booking_choice_list(db: Session, bookings: list[Booking]) -> str:
    tz_name = get_display_timezone(db)
    lines = ["Уточните, пожалуйста, о какой записи речь:"]
    for i, booking in enumerate(bookings, start=1):
        client = booking.client if booking.client is not None else db.get(Client, int(booking.client_id))
        name = (client.name if client else "") or "—"
        when = ""
        if booking.planned_date is not None:
            when = format_naive_utc_datetime(booking.planned_date, tz_name, "%d.%m.%Y %H:%M")
        lines.append(f"{i} — {name}, {when}".rstrip(", "))
    return "\n".join(lines)


def _get_active_pending(
    db: Session, *, channel: str, messenger_id: int
) -> ClientMessengerPending | None:
    now = utcnow_naive()
    row = db.scalar(
        select(ClientMessengerPending)
        .where(
            ClientMessengerPending.channel == channel.strip().lower(),
            ClientMessengerPending.messenger_user_id == int(messenger_id),
            ClientMessengerPending.expires_at > now,
        )
        .order_by(ClientMessengerPending.id.desc())
        .limit(1)
    )
    return row


def _clear_pending(db: Session, *, channel: str, messenger_id: int) -> None:
    rows = list(
        db.scalars(
            select(ClientMessengerPending).where(
                ClientMessengerPending.channel == channel.strip().lower(),
                ClientMessengerPending.messenger_user_id == int(messenger_id),
            )
        ).all()
    )
    for row in rows:
        db.delete(row)
    db.flush()


def _set_pending(
    db: Session,
    *,
    client: Client,
    channel: str,
    messenger_id: int,
    kind: str,
    payload: dict | None = None,
    booking_id: int | None = None,
) -> ClientMessengerPending:
    _clear_pending(db, channel=channel, messenger_id=messenger_id)
    now = utcnow_naive()
    row = ClientMessengerPending(
        client_id=int(client.id),
        channel=channel.strip().lower(),
        messenger_user_id=int(messenger_id),
        kind=kind,
        payload_json=json.dumps(payload or {}, ensure_ascii=False),
        booking_id=int(booking_id) if booking_id else None,
        created_at=now,
        expires_at=now + timedelta(minutes=PENDING_TTL_MINUTES),
    )
    db.add(row)
    db.flush()
    return row


def _contact_reply_suffix(sender: Client, booking: Booking) -> str:
    if int(booking.client_id) == int(sender.id):
        return ""
    name = (sender.name or "").strip() or "—"
    return f"\nОтветило контактное лицо: {name}"


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


def _build_cancel_request_text(
    db: Session, booking: Booking, client: Client, channel: str, *, sender: Client | None = None
) -> str:
    tz = get_display_timezone(db)
    marker = get_services_marker(db)
    when = format_naive_utc_datetime(booking.planned_date, tz) or "—"
    services = _services_block(booking, marker, with_duration=True) or "—"
    masters = _masters_csv(booking) or "—"
    via = _channel_label(channel)
    booking_client = (
        booking.client if booking.client is not None else db.get(Client, int(booking.client_id))
    )
    client_name = (booking_client.name if booking_client else None) or (client.name or "—")
    extra = _contact_reply_suffix(sender, booking) if sender is not None else ""
    return (
        f"Клиент просит отменить запись\n"
        f"Клиент: {client_name}\n"
        f"Когда: {when}\n"
        f"Услуги:\n{services}\n"
        f"Мастера: {masters}\n"
        f"Канал ответа: {via}\n"
        f"Бронь: /bookings/{int(booking.id)}"
        f"{extra}"
    )


def _enqueue_cancel_request_notices(
    db: Session,
    booking: Booking,
    client: Client,
    *,
    channel: str,
    sender: Client | None = None,
) -> None:
    text = _build_cancel_request_text(db, booking, client, channel, sender=sender or client)
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
                    body=text,
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
                body=text,
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
    services = _services_block(booking, marker, with_duration=True) or "—"
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
                body=text,
                status=NotificationOutboxStatus.PENDING,
                dedupe_key=key,
                attempt_count=0,
                created_at=utcnow_naive(),
            )
        )
    db.flush()


def _enqueue_reschedule_admin(
    db: Session, booking: Booking, client: Client, *, channel: str, sender: Client | None = None
) -> None:
    via = _channel_label(channel)
    who = sender or client
    booking_client = (
        booking.client if booking.client is not None else db.get(Client, int(booking.client_id))
    )
    facts_client = booking_client or client
    text = (
        "Клиентка просит перенести запись\n"
        f"{_booking_facts(db, booking, facts_client)}\n"
        f"Канал ответа: {via}"
        f"{_contact_reply_suffix(who, booking)}"
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
    """Обработать текст от клиента. Возвращает ответ или None (если не клиент / пусто).

    Один и тот же аккаунт может быть и у сотрудницы, и у клиентки. Тогда цифры и
    прочий текст идут в ответ клиентки, только если у неё есть подходящая бронь.
    Иначе сообщение остаётся поведением сотрудницы: сейчас это игнор.
    Коды привязки разбираются раньше, в вебхуке, и сюда не попадают.
    """
    staff = find_user_by_channel_id(db, channel, messenger_id)
    client = find_client_by_channel_id(db, channel, messenger_id)
    if staff is not None and (
        client is None or not suitable_bookings_for_reply_circle(db, client)
    ):
        return None
    if client is None:
        return _log_client_inbound(
            db,
            client_id=None,
            booking_id=None,
            channel=channel,
            messenger_id=messenger_id,
            text=text,
            parsed_action="unknown",
            answer=None,
            reply_key=None,
        )
    reply = normalize_client_reply(text)
    ch = channel.strip().lower()

    # Pending: выбор брони из списка (цифра = номер пункта, не «Цифры ответов»).
    pending = _get_active_pending(db, channel=ch, messenger_id=int(messenger_id))
    if pending is not None and pending.kind == PENDING_KIND_CHOICE:
        suitable = suitable_bookings_for_reply_circle(db, client)
        if not _reply_needs_booking_choice(suitable):
            _clear_pending(db, channel=ch, messenger_id=int(messenger_id))
            pending = None
        else:
            try:
                payload = json.loads(pending.payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            options = payload.get("booking_ids") or [int(b.id) for b in suitable]
            if reply is None or not str(reply).isdigit():
                answer = _format_booking_choice_list(db, suitable)
                _set_pending(
                    db,
                    client=client,
                    channel=ch,
                    messenger_id=int(messenger_id),
                    kind=PENDING_KIND_CHOICE,
                    payload={"booking_ids": [int(b.id) for b in suitable]},
                )
                return _log_client_inbound(
                    db,
                    client_id=int(client.id),
                    booking_id=None,
                    channel=ch,
                    messenger_id=messenger_id,
                    text=text,
                    parsed_action="unknown",
                    answer=answer,
                    reply_key="hint",
                )
            idx = int(reply)
            if idx < 1 or idx > len(options):
                answer = _format_booking_choice_list(db, suitable)
                return _log_client_inbound(
                    db,
                    client_id=int(client.id),
                    booking_id=None,
                    channel=ch,
                    messenger_id=messenger_id,
                    text=text,
                    parsed_action="unknown",
                    answer=answer,
                    reply_key="hint",
                )
            chosen_id = int(options[idx - 1])
            _set_pending(
                db,
                client=client,
                channel=ch,
                messenger_id=int(messenger_id),
                kind=PENDING_KIND_SELECTED,
                booking_id=chosen_id,
                payload={"booking_id": chosen_id},
            )
            return _log_client_inbound(
                db,
                client_id=int(client.id),
                booking_id=chosen_id,
                channel=ch,
                messenger_id=messenger_id,
                text=text,
                parsed_action="unknown",
                answer="Запись выбрана. Пожалуйста, повторите ваш ответ (цифру из подсказки).",
                reply_key="hint",
            )

    preferred_booking_id: int | None = None
    if pending is not None and pending.kind == PENDING_KIND_SELECTED and pending.booking_id:
        preferred_booking_id = int(pending.booking_id)

    action = action_for_digit(db, reply) if reply is not None else None
    parsed = action or "unknown"
    if reply is None or action is None:
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=None,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action="unknown",
            answer=build_reply_hint(db),
            reply_key="hint",
        )

    # Связь с админом и «как добраться» не требуют активной брони.
    if action == ACTION_CONTACT:
        try:
            _enqueue_contact_admin(db, client, channel=ch, messenger_id=int(messenger_id))
        except Exception:
            logger.exception("messenger_inbound: contact notify enqueue failed client=%s", client.id)
        key = contact_reply_key(ch)
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=None,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action=ACTION_CONTACT,
            answer=render_bot_reply(db, key),
            reply_key=key,
        )
    if action == ACTION_DIRECTIONS:
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=None,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action=ACTION_DIRECTIONS,
            answer=render_bot_reply(db, "directions_5"),
            reply_key="directions_5",
        )

    suitable = suitable_bookings_for_reply_circle(db, client)
    if preferred_booking_id is None and _reply_needs_booking_choice(suitable):
        answer = _format_booking_choice_list(db, suitable)
        _set_pending(
            db,
            client=client,
            channel=ch,
            messenger_id=int(messenger_id),
            kind=PENDING_KIND_CHOICE,
            payload={"booking_ids": [int(b.id) for b in suitable]},
        )
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=None,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action=parsed,
            answer=answer,
            reply_key="hint",
        )

    booking = find_booking_for_client_reply(db, client, preferred_booking_id=preferred_booking_id)
    if booking is None:
        if preferred_booking_id is not None:
            _clear_pending(db, channel=ch, messenger_id=int(messenger_id))
        if action == ACTION_RESCHEDULE:
            contact_digit = digit_for_action(db, ACTION_CONTACT, only_enabled=True) or "4"
            answer = render_bot_reply(db, "no_booking_reschedule")
            if "4" in answer and contact_digit != "4":
                answer = answer.replace("4", contact_digit, 1)
            key = "no_booking_reschedule"
        else:
            answer = render_bot_reply(db, "no_booking")
            key = "no_booking"
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=None,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action=parsed,
            answer=answer,
            reply_key=key,
        )

    booking = _load_booking_graph(db, booking)
    now = utcnow_naive()
    booking_id = int(booking.id)
    if action == ACTION_CONFIRM:
        if booking.client_confirmed_at is not None:
            return _log_client_inbound(
                db,
                client_id=int(client.id),
                booking_id=booking_id,
                channel=ch,
                messenger_id=messenger_id,
                text=text,
                parsed_action=ACTION_CONFIRM,
                answer=MSG_ALREADY_CONFIRMED,
                reply_key="already_confirmed",
            )
        booking.client_confirmed_at = now
        booking.client_confirmed_via = ch
        db.flush()
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=booking_id,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action=ACTION_CONFIRM,
            answer=render_bot_reply(db, "confirm_1"),
            reply_key="confirm_1",
        )
    if action == ACTION_RESCHEDULE:
        repeat = booking.client_reschedule_requested_at is not None
        booking.client_reschedule_requested_at = now
        booking.client_reschedule_requested_via = ch
        db.flush()
        if not repeat:
            try:
                _enqueue_reschedule_admin(db, booking, client, channel=ch, sender=client)
            except Exception:
                logger.exception(
                    "messenger_inbound: reschedule notify enqueue failed booking=%s", booking.id
                )
        return _log_client_inbound(
            db,
            client_id=int(client.id),
            booking_id=booking_id,
            channel=ch,
            messenger_id=messenger_id,
            text=text,
            parsed_action=ACTION_RESCHEDULE,
            answer=render_bot_reply(db, "reschedule_2"),
            reply_key="reschedule_2",
        )

    # cancel — логика отмены как раньше, текст из настройки.
    booking.client_cancel_requested_at = now
    booking.client_cancel_requested_via = ch
    db.flush()
    try:
        _enqueue_cancel_request_notices(db, booking, client, channel=ch, sender=client)
    except Exception:
        logger.exception("messenger_inbound: cancel notify enqueue failed booking=%s", booking.id)
    return _log_client_inbound(
        db,
        client_id=int(client.id),
        booking_id=booking_id,
        channel=ch,
        messenger_id=messenger_id,
        text=text,
        parsed_action=ACTION_CANCEL,
        answer=render_bot_reply(db, "cancel_3"),
        reply_key="cancel_3",
    )


def process_inbound_message(
    db: Session,
    *,
    channel: str,
    messenger_id: int,
    text: str | None,
) -> None:
    """Обработать и ответить клиенту. Ошибки глотает."""
    try:
        db.info.pop("lb_bot_reply", None)
        answer = handle_client_text(db, channel=channel, messenger_id=messenger_id, text=text)
        meta = db.info.pop("lb_bot_reply", None)
        db.commit()
        if answer:
            status = NotificationOutboxStatus.SENT
            error: str | None = None
            try:
                reply_to_messenger(channel, messenger_id, answer)
            except Exception as e:
                status = NotificationOutboxStatus.FAILED
                error = str(e)[:2000]
                logger.exception(
                    "messenger_inbound: reply failed channel=%s id=%s", channel, messenger_id
                )
            if isinstance(meta, dict):
                try:
                    _save_bot_reply_outbox(db, meta, status=status, error=error, text=answer)
                    db.commit()
                except Exception:
                    logger.exception("messenger_inbound: bot reply log failed")
                    try:
                        db.rollback()
                    except Exception:
                        pass
                if status == NotificationOutboxStatus.SENT:
                    try:
                        from app.route_video_send import deliver_route_video_for_bot_reply

                        deliver_route_video_for_bot_reply(db, meta)
                        db.commit()
                    except Exception:
                        logger.exception("messenger_inbound: route video failed")
                        try:
                            db.rollback()
                        except Exception:
                            pass
    except Exception:
        logger.exception("messenger_inbound: handler error channel=%s", channel)
        try:
            db.rollback()
        except Exception:
            pass
