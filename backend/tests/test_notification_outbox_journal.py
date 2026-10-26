"""3.51: колонки и статусы журнала очереди уведомлений."""

from __future__ import annotations

import json

from app.booking_status_notify import SKIP_NO_CHANNEL, SKIP_PAST
from app.db.models import (
    Client,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserRole,
)
from app.routes.notification_outbox import _recipient, _status_ru, _subject_links


def _row(**kwargs) -> NotificationOutbox:
    data = {
        "target_kind": "user",
        "channel": NotificationChannel.TELEGRAM,
        "event_type": "booking_created",
        "payload_json": "{}",
        "status": NotificationOutboxStatus.SENT,
        "dedupe_key": "k",
        "attempt_count": 0,
    }
    data.update(kwargs)
    return NotificationOutbox(**data)


def test_subject_is_visit_for_staff_assignment_not_deleted_booking() -> None:
    row = _row(
        event_type="staff_assignment_created",
        booking_id=None,
        payload_json=json.dumps({"entity_type": "visit", "entity_id": 12}),
    )
    assert _subject_links(row) == [{"label": "Визит №12", "href": "/visits/12"}]


def test_subject_booking_visit_and_removed() -> None:
    booking = _row(booking_id=486, event_type="client_booking_status", target_kind="client")
    assert _subject_links(booking) == [{"label": "Бронь №486", "href": "/bookings/486"}]

    both = _row(booking_id=5, visit_id=9, event_type="client_after_booking", target_kind="client")
    assert _subject_links(both) == [
        {"label": "Визит №9", "href": "/visits/9"},
        {"label": "Бронь №5", "href": "/bookings/5"},
    ]

    removed = _row(
        booking_id=None,
        target_kind="client",
        payload_json=json.dumps({"booking_id": 4}),
    )
    assert _subject_links(removed) == [{"label": "бронь удалена"}]

    work = _row(payload_json=json.dumps({"entity_type": "work", "entity_id": 3}))
    assert _subject_links(work) == [{"label": "Работа №3", "href": "/sales/work/3"}]

    assert _subject_links(_row(event_type="client_contact_request", target_kind="client")) == []


def test_recipient_client_links_and_staff_is_marked() -> None:
    client = Client(id=7, name="Екатерина")
    row = _row(target_kind="client", client_id=7, client=client)
    assert _recipient(row, client_removed=False) == {
        "name": "Екатерина",
        "mark": "клиент",
        "href": "/clients/7",
    }

    staff = User(
        username="olya",
        password_hash="x",
        display_name="Олеся",
        role=UserRole.MASTER,
    )
    staff_row = _row(target_kind="user", user=staff)
    assert _recipient(staff_row, client_removed=False)["mark"] == "сотрудник"
    assert _recipient(staff_row, client_removed=False)["href"] == ""
    assert _recipient(staff_row, client_removed=False)["name"] == "Олеся"

    chat = _row(target_kind="admin_chat", user_id=None)
    assert _recipient(chat, client_removed=False)["mark"] == "чат"


def test_skip_is_not_delivery_error_and_failure_is() -> None:
    assert _status_ru(NotificationOutboxStatus.CANCELLED, skip_reason=SKIP_NO_CHANNEL) == (
        "не отправлено: нет канала"
    )
    assert _status_ru(NotificationOutboxStatus.CANCELLED, skip_reason=SKIP_PAST) == (
        "не отправлено: время записи уже прошло"
    )
    assert _status_ru(NotificationOutboxStatus.FAILED) == "ошибка доставки"
    assert _status_ru(NotificationOutboxStatus.CANCELLED, skip_reason="пропущено: бронь удалена") == "отменено"
    assert _status_ru(NotificationOutboxStatus.SENT) == "отправлено"
