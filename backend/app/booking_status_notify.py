"""Уведомление клиенту при смене статуса брони.

Срабатывает из одного места: перед flush сессии, если у брони изменился status.
Напоминания и «после визита» здесь не трогаются.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, selectinload
from sqlalchemy.orm.attributes import NEVER_SET, NO_VALUE

from app.care_memos import MEMO_BEFORE, care_memo_ids_for_booking, memo_names
from app.client_booking_notify import _client_channel_targets
from app.client_notifications import get_services_marker, parse_template, render_client_notify_template
from app.consultation_booking import booking_status_label
from app.db.models import (
    Booking,
    BookingMaster,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    BookingStatus,
    BookingStatusNotifyRule,
    Client,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
)
from app.display_time import get_display_timezone
from app.route_video_send import template_wants_route_video
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

EVENT_CLIENT_BOOKING_STATUS = "client_booking_status"

DEFAULT_CONFIRMED_TEMPLATE = (
    "Здравствуйте, {{name}}! Ваша запись подтверждена: {{date_text}} в {{time}}, "
    "{{services}}. Ждём вас по адресу: {{address}}\n\n{{reply_hint_no_confirm}}"
)
DEFAULT_STATUS_TEMPLATE = (
    "Здравствуйте, {{name}}! {{date_text}} в {{time}}, {{services}}. "
    "Ждём вас по адресу: {{address}}\n\n{{reply_hint}}"
)

SKIP_PAST = "время записи уже прошло"
SKIP_DISABLED = "отключено"
SKIP_ALREADY = "уже отправлено"
SKIP_NO_CHANNEL = "нет канала"

_FIRED = (
    NotificationOutboxStatus.PENDING,
    NotificationOutboxStatus.SENDING,
    NotificationOutboxStatus.SENT,
    NotificationOutboxStatus.FAILED,
)


def _status_value(value: object) -> str:
    if value is None:
        return ""
    raw = value.value if hasattr(value, "value") else value
    return str(raw or "").strip()


def status_choices() -> list[dict[str, str]]:
    return [{"value": item.value, "label": booking_status_label(item)} for item in BookingStatus]


def list_status_rules(db: Session) -> list[BookingStatusNotifyRule]:
    return list(
        db.scalars(
            select(BookingStatusNotifyRule).order_by(
                BookingStatusNotifyRule.position.asc(),
                BookingStatusNotifyRule.id.asc(),
            )
        ).all()
    )


def ensure_default_status_rules(db: Session) -> bool:
    """Пустая таблица — одно правило «подтверждена». Иначе ничего не создаём."""
    if db.scalar(select(BookingStatusNotifyRule.id).limit(1)) is not None:
        return False
    now = utcnow_naive()
    db.add(
        BookingStatusNotifyRule(
            status=BookingStatus.ACTIVE.value,
            template=DEFAULT_CONFIRMED_TEMPLATE,
            attach_memo_before=True,
            is_enabled=True,
            position=0,
            created_at=now,
        )
    )
    db.flush()
    return True


def status_rules_for_ui(db: Session) -> dict[str, Any]:
    from app.client_reply_digits import template_has_manual_reply_digits

    rows = list_status_rules(db)
    used = {_status_value(row.status) for row in rows}
    ui_rows: list[dict[str, Any]] = []
    for row in rows:
        current = _status_value(row.status)
        options = [
            item
            for item in status_choices()
            if item["value"] == current or item["value"] not in used
        ]
        ui_rows.append(
            {
                "id": int(row.id),
                "status": current,
                "template": row.template,
                "is_enabled": bool(row.is_enabled),
                "attach_memo_before": bool(row.attach_memo_before),
                "position": int(row.position),
                "status_options": options,
                "manual_digits_warning": template_has_manual_reply_digits(row.template),
            }
        )
    free = [item for item in status_choices() if item["value"] not in used]
    return {"rules": ui_rows, "free_statuses": free}


def _parse_status(raw: Any) -> str:
    try:
        return BookingStatus(str(raw or "").strip()).value
    except ValueError as exc:
        raise ValueError("Выберите статус брони.") from exc


def save_status_rules_from_form(
    db: Session,
    *,
    ids: Sequence[Any],
    statuses: Sequence[Any],
    templates: Sequence[Any],
    enabled_flags: Sequence[Any],
    memo_flags: Sequence[Any],
) -> None:
    if not ids:
        return
    if not (
        len(ids) == len(statuses) == len(templates) == len(enabled_flags) == len(memo_flags)
    ):
        raise ValueError("Не удалось сохранить правила по статусам.")
    seen: set[str] = set()
    parsed: list[tuple[int, str, str, bool, bool]] = []
    for raw_id, raw_status, raw_template, raw_enabled, raw_memo in zip(
        ids, statuses, templates, enabled_flags, memo_flags, strict=True
    ):
        status = _parse_status(raw_status)
        if status in seen:
            raise ValueError("Один статус можно выбрать только в одном правиле.")
        seen.add(status)
        try:
            rule_id = int(str(raw_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("Правило не найдено.") from exc
        parsed.append(
            (
                rule_id,
                status,
                parse_template(raw_template),
                str(raw_enabled) == "1",
                str(raw_memo) == "1",
            )
        )
    now = utcnow_naive()
    for rule_id, status, template, enabled, memo in parsed:
        row = db.get(BookingStatusNotifyRule, rule_id)
        if row is None:
            raise ValueError("Правило не найдено.")
        row.status = status
        row.template = template
        row.is_enabled = enabled
        row.attach_memo_before = memo
        row.updated_at = now
    try:
        db.flush()
    except IntegrityError as exc:
        raise ValueError("Один статус можно выбрать только в одном правиле.") from exc


def add_status_rule(db: Session, status_raw: Any) -> BookingStatusNotifyRule:
    status = _parse_status(status_raw)
    taken = db.scalar(
        select(BookingStatusNotifyRule.id).where(BookingStatusNotifyRule.status == status).limit(1)
    )
    if taken is not None:
        raise ValueError("Для этого статуса правило уже есть.")
    rows = list_status_rules(db)
    now = utcnow_naive()
    template = DEFAULT_CONFIRMED_TEMPLATE if status == BookingStatus.ACTIVE.value else DEFAULT_STATUS_TEMPLATE
    row = BookingStatusNotifyRule(
        status=status,
        template=template,
        attach_memo_before=True,
        is_enabled=True,
        position=len(rows),
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def delete_status_rule(db: Session, rule_id: int) -> None:
    row = db.get(BookingStatusNotifyRule, int(rule_id))
    if row is None:
        raise ValueError("Правило не найдено.")
    db.delete(row)
    db.flush()
    now = utcnow_naive()
    for index, item in enumerate(list_status_rules(db)):
        item.position = index
        item.updated_at = now
    db.flush()


def move_status_rule(db: Session, rule_id: int, *, direction: str) -> None:
    rows = list_status_rules(db)
    idx = next((i for i, row in enumerate(rows) if int(row.id) == int(rule_id)), None)
    if idx is None:
        raise ValueError("Правило не найдено.")
    step = (direction or "").strip().lower()
    if step == "up" and idx > 0:
        rows[idx], rows[idx - 1] = rows[idx - 1], rows[idx]
    elif step == "down" and idx < len(rows) - 1:
        rows[idx], rows[idx + 1] = rows[idx + 1], rows[idx]
    else:
        return
    now = utcnow_naive()
    for index, row in enumerate(rows):
        row.position = index
        row.updated_at = now
    db.flush()


def _planned_version(booking: Booking) -> str:
    planned = booking.planned_date
    if planned is None:
        return "none"
    return planned.strftime("%Y%m%d%H%M%S")


def _send_key(booking_id: int, rule_id: int, version: str, channel: str) -> str:
    return f"bsn:{booking_id}:{rule_id}:{version}:{channel}"


def _skip_key(booking_id: int, rule_id: int, version: str, code: str) -> str:
    return f"bsn-skip:{booking_id}:{rule_id}:{version}:{code}"


def _already_fired(db: Session, booking_id: int, rule_id: int, version: str) -> bool:
    prefix = f"bsn:{int(booking_id)}:{int(rule_id)}:{version}:"
    found = db.scalar(
        select(NotificationOutbox.id)
        .where(
            NotificationOutbox.dedupe_key.like(prefix + "%"),
            NotificationOutbox.status.in_(_FIRED),
        )
        .limit(1)
    )
    return found is not None


def _insert_outbox(db: Session, row: NotificationOutbox) -> None:
    """Без flush: вызов идёт из before_flush, вложенный flush отправил бы статус дважды."""
    prefix = row.dedupe_key
    exists = db.scalar(
        select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == prefix).limit(1)
    )
    if exists is not None:
        return
    db.add(row)


def _skip(
    db: Session,
    *,
    booking: Booking,
    client: Client,
    rule: BookingStatusNotifyRule,
    reason: str,
    version: str,
) -> None:
    label = booking_status_label(BookingStatus(rule.status))
    key = _skip_key(int(booking.id), int(rule.id), version, reason)
    payload = {
        "text": "",
        "target_kind": "client",
        "event_type": EVENT_CLIENT_BOOKING_STATUS,
        "booking_id": int(booking.id),
        "status_rule_id": int(rule.id),
        "status": rule.status,
        "status_label": label,
        "skip_reason": reason,
        "planned_version": version,
    }
    _insert_outbox(
        db,
        NotificationOutbox(
            user_id=None,
            client_id=int(client.id),
            target_kind="client",
            booking_id=int(booking.id),
            channel=NotificationChannel.TELEGRAM,
            event_type=EVENT_CLIENT_BOOKING_STATUS,
            payload_json=json.dumps(payload, ensure_ascii=False),
            body="",
            rule_id=None,
            status=NotificationOutboxStatus.CANCELLED,
            error=reason,
            dedupe_key=key,
            attempt_count=0,
            created_at=utcnow_naive(),
            sent_at=None,
        ),
    )


def enqueue_booking_status_notification(db: Session, booking: Booking, *, new_status: object) -> None:
    """Поставить сообщение в outbox или записать, почему оно не ушло.

    no_promo не блокирует: напоминания о записи его тоже не учитывают.
    """
    if booking.id is None or booking.client_id is None:
        return
    status = _status_value(new_status)
    if not status:
        return
    rule = db.scalar(
        select(BookingStatusNotifyRule).where(BookingStatusNotifyRule.status == status).limit(1)
    )
    if rule is None:
        return
    client = db.get(Client, int(booking.client_id))
    if client is None:
        return
    loaded = db.scalar(
        select(Booking)
        .where(Booking.id == int(booking.id))
        .options(
            selectinload(Booking.masters).selectinload(BookingMaster.master),
            selectinload(Booking.planned_service),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.masters)
            .selectinload(BookingPlannedServiceMaster.master),
        )
    )
    if loaded is not None:
        booking = loaded
    version = _planned_version(booking)
    planned = booking.planned_date
    if planned is None or planned <= utcnow_naive():
        _skip(db, booking=booking, client=client, rule=rule, reason=SKIP_PAST, version=version)
        return
    if not bool(rule.is_enabled):
        _skip(db, booking=booking, client=client, rule=rule, reason=SKIP_DISABLED, version=version)
        return
    if _already_fired(db, int(booking.id), int(rule.id), version):
        _skip(db, booking=booking, client=client, rule=rule, reason=SKIP_ALREADY, version=version)
        return
    from app.client_contact import resolve_client_notify_targets_db

    targets, via_contact = resolve_client_notify_targets_db(db, client)
    if not targets:
        _skip(db, booking=booking, client=client, rule=rule, reason=SKIP_NO_CHANNEL, version=version)
        return

    tz_name = get_display_timezone(db)
    marker = get_services_marker(db)
    text = render_client_notify_template(
        rule.template,
        booking=booking,
        client=client,
        tz_name=tz_name,
        marker=marker,
        db=db,
        substitute_system=True,
        via_contact=via_contact,
    )
    wants_video = template_wants_route_video(rule.template)
    memo_ids: list[int] = []
    names: list[str] = []
    if bool(rule.attach_memo_before):
        memo_ids = care_memo_ids_for_booking(db, booking, kind=MEMO_BEFORE)
        names = memo_names(db, memo_ids)
    if not (text or "").strip() and not wants_video and not memo_ids:
        return

    label = booking_status_label(BookingStatus(rule.status))
    for channel, target_id in targets:
        payload = {
            "text": text,
            "target_id": int(target_id),
            "target_kind": "client",
            "event_type": EVENT_CLIENT_BOOKING_STATUS,
            "booking_id": int(booking.id),
            "status_rule_id": int(rule.id),
            "status": rule.status,
            "status_label": label,
            "planned_version": version,
        }
        if via_contact is not None:
            payload["via_contact_client_id"] = int(via_contact.id)
            payload["via_contact_name"] = (via_contact.name or "").strip()
        if wants_video:
            payload["attach_route_video"] = True
        if memo_ids:
            payload["care_memo_ids"] = memo_ids
            payload["care_memos_attached"] = names
        _insert_outbox(
            db,
            NotificationOutbox(
                user_id=None,
                client_id=int(client.id),
                target_kind="client",
                booking_id=int(booking.id),
                channel=channel,
                event_type=EVENT_CLIENT_BOOKING_STATUS,
                payload_json=json.dumps(payload, ensure_ascii=False),
                body=text,
                rule_id=None,
                status=NotificationOutboxStatus.PENDING,
                error=None,
                dedupe_key=_send_key(int(booking.id), int(rule.id), version, channel.value),
                attempt_count=0,
                created_at=utcnow_naive(),
                sent_at=None,
            ),
        )


def _remember_status_change(target: Booking, value: object, oldvalue: object) -> None:
    """Запомнить смену статуса. После commit объект expired, и история атрибута старое значение теряет."""
    if getattr(target, "id", None) is None:
        return
    old = oldvalue
    if old is NO_VALUE or old is NEVER_SET:
        session = object_session(target)
        if session is None:
            return
        with session.no_autoflush:
            old = session.execute(
                select(Booking.__table__.c.status).where(Booking.__table__.c.id == int(target.id))
            ).scalar_one_or_none()
    old_status = _status_value(old)
    new_status = _status_value(value)
    if not old_status or not new_status or old_status == new_status:
        target.__dict__.pop("_bsn_new_status", None)
        return
    target.__dict__["_bsn_new_status"] = new_status


@event.listens_for(Booking.status, "set")
def _on_booking_status_set(target: Booking, value: object, oldvalue: object, initiator: object) -> None:
    _ = initiator
    try:
        _remember_status_change(target, value, oldvalue)
    except Exception:
        logger.exception("booking status notify: не удалось запомнить статус booking_id=%s", getattr(target, "id", None))


def _collect_status_changes(session: Session) -> list[tuple[Booking, str]]:
    found: list[tuple[Booking, str]] = []
    for obj in list(session.dirty):
        if not isinstance(obj, Booking):
            continue
        new_status = obj.__dict__.get("_bsn_new_status")
        if not new_status:
            continue
        found.append((obj, str(new_status)))
        obj.__dict__.pop("_bsn_new_status", None)
    return found


@event.listens_for(Session, "before_flush")
def _enqueue_on_booking_status_change(session: Session, flush_context: object, instances: object) -> None:
    """Любая смена Booking.status попадает сюда, в том числе из форм и автозакрытия."""
    _ = flush_context, instances
    if session.info.get("bsn_in_flush"):
        return
    session.info["bsn_in_flush"] = True
    try:
        try:
            changes = _collect_status_changes(session)
        except Exception:
            logger.exception("booking status notify: не удалось прочитать смену статуса")
            return
        for booking, new_status in changes:
            try:
                enqueue_booking_status_notification(session, booking, new_status=new_status)
            except Exception:
                logger.exception(
                    "booking status notify: не поставлено booking_id=%s status=%s",
                    getattr(booking, "id", None),
                    new_status,
                )
    finally:
        session.info["bsn_in_flush"] = False
