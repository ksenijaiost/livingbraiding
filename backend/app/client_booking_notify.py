"""Постановка клиентских напоминаний before/after в notification_outbox."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.client_notifications import (
    KIND_AFTER,
    KIND_BEFORE,
    get_services_marker,
    list_rules,
    render_client_notify_template,
)
from app.consultation_booking import OPEN_BOOKING_STATUSES
from app.db.models import (
    Booking,
    BookingPlannedService,
    Client,
    ClientNotificationRule,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
)
from app.display_time import get_display_timezone
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

EVENT_CLIENT_BOOKING_REMINDER = "client_booking_reminder"
EVENT_CLIENT_AFTER_BOOKING = "client_after_booking"

CLIENT_REMINDER_LATENESS_WINDOW = timedelta(hours=2)
MAX_CLIENT_RULE_HOURS = 720


def _planned_version(booking: Booking) -> str:
    pd = booking.planned_date
    if pd is None:
        return "none"
    return pd.strftime("%Y%m%d%H%M%S")


def _client_channel_targets(client: Client) -> list[tuple[NotificationChannel, int]]:
    out: list[tuple[NotificationChannel, int]] = []
    if client.vk_user_id is not None:
        out.append((NotificationChannel.VK, int(client.vk_user_id)))
    if client.max_user_id is not None:
        out.append((NotificationChannel.MAX, int(client.max_user_id)))
    if client.telegram_chat_id is not None:
        out.append((NotificationChannel.TELEGRAM, int(client.telegram_chat_id)))
    return out


def _dedupe_key(
    *,
    booking_id: int,
    client_id: int,
    channel: NotificationChannel,
    rule_id: int,
    planned_version: str,
) -> str:
    return f"cr:{booking_id}:{client_id}:{channel.value}:{rule_id}:{planned_version}"


def client_rule_is_due(
    *,
    kind: str,
    planned_date: datetime,
    hours: int,
    now: datetime,
    lateness_window: timedelta = CLIENT_REMINDER_LATENESS_WINDOW,
) -> bool:
    """before: fire_at = planned − hours; after: fire_at = planned + hours."""
    if planned_date is None:
        return False
    h = int(hours)
    if kind == KIND_BEFORE.value:
        if planned_date <= now:
            return False
        fire_at = planned_date - timedelta(hours=h)
        if fire_at > now:
            return False
        if now - fire_at > lateness_window:
            return False
        return True
    # after_booking
    fire_at = planned_date + timedelta(hours=h)
    if fire_at > now:
        return False
    if now - fire_at > lateness_window:
        return False
    return True


def enqueue_client_rule_notifications(
    db: Session,
    booking: Booking,
    client: Client,
    rule: ClientNotificationRule,
    *,
    now: datetime | None = None,
) -> list[NotificationOutbox]:
    """Поставить outbox-строки по правилу во все каналы клиента."""
    _ = now
    if not rule.is_enabled:
        return []
    targets = _client_channel_targets(client)
    if not targets:
        return []
    tz_name = get_display_timezone(db)
    marker = get_services_marker(db)
    if rule.kind == KIND_BEFORE:
        text = render_client_notify_template(
            rule.template,
            booking=booking,
            client=client,
            tz_name=tz_name,
            marker=marker,
        )
        event_type = EVENT_CLIENT_BOOKING_REMINDER
    else:
        text = rule.template  # after — без подстановки переменных
        event_type = EVENT_CLIENT_AFTER_BOOKING
    if not (text or "").strip():
        return []

    version = _planned_version(booking)
    created: list[NotificationOutbox] = []
    for channel, target_id in targets:
        key = _dedupe_key(
            booking_id=int(booking.id),
            client_id=int(client.id),
            channel=channel,
            rule_id=int(rule.id),
            planned_version=version,
        )
        exists = db.scalar(
            select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)
        )
        if exists is not None:
            continue
        payload = {
            "text": text,
            "target_id": target_id,
            "target_kind": "client",
            "event_type": event_type,
            "booking_id": int(booking.id),
            "rule_id": int(rule.id),
        }
        row = NotificationOutbox(
            user_id=None,
            client_id=int(client.id),
            target_kind="client",
            booking_id=int(booking.id),
            channel=channel,
            event_type=event_type,
            payload_json=json.dumps(payload, ensure_ascii=False),
            status=NotificationOutboxStatus.PENDING,
            error=None,
            dedupe_key=key,
            attempt_count=0,
            created_at=utcnow_naive(),
            sent_at=None,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            continue
        created.append(row)
    return created


def enqueue_due_client_notifications(
    db: Session,
    *,
    now: datetime | None = None,
    limit_bookings: int = 200,
) -> dict[str, int]:
    """Найти due before/after по правилам и поставить в outbox."""
    now = now or utcnow_naive()
    rules = [r for r in list_rules(db) if r.is_enabled]
    if not rules:
        return {"checked": 0, "enqueued": 0, "bookings": 0}

    max_h = max((int(r.hours) for r in rules), default=0)
    max_h = min(max(max_h, 0), MAX_CLIENT_RULE_HOURS)
    # before: planned в (now, now+max_h]; after: planned в [now-max_h-window, now]
    lower = now - timedelta(hours=max_h) - CLIENT_REMINDER_LATENESS_WINDOW
    upper = now + timedelta(hours=max_h) + CLIENT_REMINDER_LATENESS_WINDOW

    bookings = list(
        db.scalars(
            select(Booking)
            .where(
                Booking.status.in_(tuple(OPEN_BOOKING_STATUSES)),
                Booking.planned_date >= lower,
                Booking.planned_date <= upper,
            )
            .options(
                selectinload(Booking.client),
                selectinload(Booking.planned_service),
                selectinload(Booking.masters),
                selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
                selectinload(Booking.planned_services).selectinload(BookingPlannedService.masters),
            )
            .order_by(Booking.planned_date.asc())
            .limit(max(1, int(limit_bookings)))
        ).all()
    )

    stats = {"checked": 0, "enqueued": 0, "bookings": 0}
    for booking in bookings:
        stats["checked"] += 1
        client = booking.client
        if client is None:
            client = db.get(Client, int(booking.client_id))
        if client is None:
            continue
        if not _client_channel_targets(client):
            continue
        for rule in rules:
            kind_val = rule.kind.value if hasattr(rule.kind, "value") else str(rule.kind)
            if kind_val == KIND_BEFORE.value and booking.planned_date <= now:
                continue
            if not client_rule_is_due(
                kind=kind_val,
                planned_date=booking.planned_date,
                hours=int(rule.hours),
                now=now,
            ):
                continue
            rows = enqueue_client_rule_notifications(db, booking, client, rule, now=now)
            if rows:
                stats["enqueued"] += len(rows)
                stats["bookings"] += 1
    return stats
