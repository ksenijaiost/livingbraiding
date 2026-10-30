"""Сообщение «после визита»: от сохранения визита, не позже времени студии.

Напоминания «до записи» здесь не считаются.
Одна пара (правило, бронь) или (правило, визит без брони) — одно сообщение на канал.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.client_booking_notify import EVENT_CLIENT_AFTER_BOOKING, _client_channel_targets
from app.route_video_send import template_wants_route_video
from app.client_notifications import (
    KIND_AFTER,
    get_services_marker,
    list_rules,
    render_client_notify_template,
)
from app.db.models import (
    Booking,
    BookingStatus,
    Client,
    ClientNotificationRule,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    Visit,
)
from app.display_time import get_display_timezone
from app.time_utils import utc_naive_to_local, utcnow_naive


class _BareBooking:
    def __init__(self) -> None:
        self.planned_date = None
        self.planned_services: list = []
        self.masters: list = []


def studio_deadline_utc(day: date, latest: time, tz_name: str) -> datetime:
    local = datetime.combine(day, latest)
    return local.replace(tzinfo=ZoneInfo(tz_name)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def after_visit_scheduled_at(
    *,
    saved_at: datetime,
    performed_date: datetime,
    delay_hours: float,
    latest_time: time | None,
    tz_name: str,
) -> datetime:
    """Момент отправки (naive UTC).

    План = сохранение + delay. Если он позже «не позже» в день визита — в это время.
    Если визит сохранили уже после «не позже» — сразу, в момент сохранения.
    """
    plan = saved_at + timedelta(hours=float(delay_hours))
    if latest_time is None:
        return plan
    day = performed_date.date() if isinstance(performed_date, datetime) else performed_date
    deadline = studio_deadline_utc(day, latest_time, tz_name)
    if plan <= deadline:
        return plan
    if deadline <= saved_at:
        return saved_at
    return deadline


def _after_dedupe_key(
    *,
    rule_id: int,
    channel: NotificationChannel,
    booking_id: int | None,
    visit_id: int | None,
) -> str:
    ch = channel.value if hasattr(channel, "value") else str(channel)
    if booking_id:
        return f"caf:{int(rule_id)}:b{int(booking_id)}:{ch}"
    return f"caf:{int(rule_id)}:v{int(visit_id)}:{ch}"


def _render_after(
    db: Session,
    rule: ClientNotificationRule,
    client: Client,
    booking: Booking | _BareBooking,
    *,
    via_contact: Client | None = None,
) -> str:
    return render_client_notify_template(
        rule.template,
        booking=booking,  # type: ignore[arg-type]
        client=client,
        tz_name=get_display_timezone(db),
        marker=get_services_marker(db),
        db=db,
        substitute_system=False,
        system_keys=frozenset({"name", "reply_hint_general"}),
        via_contact=via_contact,
    )


def _insert_after_outbox(
    db: Session,
    *,
    client: Client,
    rule: ClientNotificationRule,
    text: str,
    channel: NotificationChannel,
    target_id: int,
    booking_id: int | None,
    visit_id: int | None,
    scheduled_at: datetime,
    care_memo_ids: list[int] | None = None,
    via_contact: Client | None = None,
) -> NotificationOutbox | None:
    key = _after_dedupe_key(
        rule_id=int(rule.id),
        channel=channel,
        booking_id=booking_id,
        visit_id=visit_id,
    )
    exists = db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1))
    if exists is not None:
        return None
    payload = {
        "text": text,
        "target_id": int(target_id),
        "target_kind": "client",
        "event_type": EVENT_CLIENT_AFTER_BOOKING,
        "booking_id": int(booking_id) if booking_id else None,
        "visit_id": int(visit_id) if visit_id else None,
        "rule_id": int(rule.id),
    }
    if template_wants_route_video(rule.template):
        payload["attach_route_video"] = True
    if care_memo_ids:
        payload["care_memo_ids"] = [int(mid) for mid in care_memo_ids]
    if via_contact is not None:
        payload["via_contact_client_id"] = int(via_contact.id)
        payload["via_contact_name"] = (via_contact.name or "").strip()
    row = NotificationOutbox(
        user_id=None,
        client_id=int(client.id),
        target_kind="client",
        booking_id=int(booking_id) if booking_id else None,
        visit_id=int(visit_id) if visit_id else None,
        channel=channel,
        event_type=EVENT_CLIENT_AFTER_BOOKING,
        payload_json=json.dumps(payload, ensure_ascii=False),
        body=text,
        rule_id=int(rule.id),
        status=NotificationOutboxStatus.PENDING,
        error=None,
        dedupe_key=key,
        attempt_count=0,
        created_at=utcnow_naive(),
        scheduled_at=scheduled_at,
        sent_at=None,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return None
    return row


def _enabled_after_rules(db: Session) -> list[ClientNotificationRule]:
    out: list[ClientNotificationRule] = []
    for rule in list_rules(db, KIND_AFTER):
        if rule.is_enabled:
            out.append(rule)
    return out


def schedule_after_visit_notifications(db: Session, visit: Visit) -> list[NotificationOutbox]:
    """Поставить отправку при создании визита. Повторный вызов и правка визита не дублируют."""
    if visit.is_cancelled or visit.id is None or visit.client_id is None:
        return []
    from app.client_contact import resolve_client_notify_targets_db

    client = db.get(Client, int(visit.client_id))
    if client is None:
        return []
    targets, via_contact = resolve_client_notify_targets_db(db, client)
    if not targets:
        return []
    rules = _enabled_after_rules(db)
    if not rules:
        return []

    booking: Booking | _BareBooking
    booking_id: int | None = int(visit.booking_id) if visit.booking_id else None
    if booking_id:
        from app.client_notifications import load_booking_for_template

        loaded = load_booking_for_template(db, booking_id)
        booking = loaded if loaded is not None else _BareBooking()
    else:
        booking = _BareBooking()

    saved_at = visit.created_at or utcnow_naive()
    tz_name = get_display_timezone(db)
    created: list[NotificationOutbox] = []
    for rule in rules:
        text = _render_after(db, rule, client, booking, via_contact=via_contact)
        if not (text or "").strip() and not template_wants_route_video(rule.template):
            continue
        scheduled_at = after_visit_scheduled_at(
            saved_at=saved_at,
            performed_date=visit.performed_date,
            delay_hours=float(rule.hours or 0),
            latest_time=rule.latest_time,
            tz_name=tz_name,
        )
        from app.care_memos import memo_ids_if_enabled

        memo_ids = memo_ids_if_enabled(db, rule, visit=visit)
        for channel, target_id in targets:
            row = _insert_after_outbox(
                db,
                client=client,
                rule=rule,
                text=text,
                channel=channel,
                target_id=target_id,
                booking_id=booking_id,
                visit_id=int(visit.id),
                scheduled_at=scheduled_at,
                care_memo_ids=memo_ids,
                via_contact=via_contact,
            )
            if row is not None:
                created.append(row)
    return created


def cancel_scheduled_after_visit(db: Session, visit: Visit) -> int:
    """Снять ещё не отправленное сообщение, если визит отменили."""
    if visit.id is None:
        return 0
    conds = [NotificationOutbox.visit_id == int(visit.id)]
    if visit.booking_id:
        conds.append(NotificationOutbox.booking_id == int(visit.booking_id))
    rows = list(
        db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == EVENT_CLIENT_AFTER_BOOKING,
                NotificationOutbox.status.in_(
                    (NotificationOutboxStatus.PENDING, NotificationOutboxStatus.FAILED)
                ),
                or_(*conds),
            )
        ).all()
    )
    for row in rows:
        row.status = NotificationOutboxStatus.CANCELLED
        row.locked_at = None
    if rows:
        db.flush()
    return len(rows)


def enqueue_due_after_booking_deadlines(
    db: Session,
    *,
    now: datetime | None = None,
    limit_bookings: int = 200,
) -> dict[str, int]:
    """Бронь сегодняшнего дня без визита: в «не позже» отправить по брони.

    Без latest_time ничего не ставит — ждём сохранение визита.
    Уже отправленное или запланированное по этой брони не дублирует.
    """
    now = now or utcnow_naive()
    rules = [r for r in _enabled_after_rules(db) if r.latest_time is not None]
    stats = {"checked": 0, "enqueued": 0}
    if not rules:
        return stats

    tz_name = get_display_timezone(db)
    local_now = utc_naive_to_local(now, tz_name)
    day = local_now.date()
    day_start = studio_deadline_utc(day, time(0, 0), tz_name)
    day_end = studio_deadline_utc(day + timedelta(days=1), time(0, 0), tz_name)

    bookings = list(
        db.scalars(
            select(Booking)
            .where(
                Booking.status != BookingStatus.CANCELLED,
                Booking.planned_date >= day_start,
                Booking.planned_date < day_end,
            )
            .order_by(Booking.planned_date.asc())
            .limit(max(1, int(limit_bookings)))
        ).all()
    )
    for booking in bookings:
        stats["checked"] += 1
        if booking.planned_date is None:
            continue
        active_visit = db.scalar(
            select(Visit.id)
            .where(Visit.booking_id == int(booking.id), Visit.is_cancelled.is_(False))
            .limit(1)
        )
        if active_visit is not None:
            continue
        from app.client_contact import resolve_client_notify_targets_db

        client = booking.client if booking.client is not None else db.get(Client, int(booking.client_id))
        if client is None:
            continue
        targets, via_contact = resolve_client_notify_targets_db(db, client)
        if not targets:
            continue
        from app.client_notifications import load_booking_for_template

        loaded = load_booking_for_template(db, int(booking.id)) or booking
        for rule in rules:
            assert rule.latest_time is not None
            deadline = studio_deadline_utc(day, rule.latest_time, tz_name)
            if now < deadline:
                continue
            text = _render_after(db, rule, client, loaded, via_contact=via_contact)
            if not (text or "").strip() and not template_wants_route_video(rule.template):
                continue
            from app.care_memos import memo_ids_if_enabled

            memo_ids = memo_ids_if_enabled(db, rule, booking=loaded)
            for channel, target_id in targets:
                row = _insert_after_outbox(
                    db,
                    client=client,
                    rule=rule,
                    text=text,
                    channel=channel,
                    target_id=target_id,
                    booking_id=int(booking.id),
                    visit_id=None,
                    scheduled_at=deadline,
                    care_memo_ids=memo_ids,
                    via_contact=via_contact,
                )
                if row is not None:
                    stats["enqueued"] += 1
    return stats
