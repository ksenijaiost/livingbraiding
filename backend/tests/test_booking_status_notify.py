"""3.33: уведомление клиенту при смене статуса брони."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.booking_status_notify import (
    DEFAULT_CONFIRMED_TEMPLATE,
    EVENT_CLIENT_BOOKING_STATUS,
    SKIP_ALREADY,
    SKIP_DISABLED,
    SKIP_NO_CHANNEL,
    SKIP_PAST,
    ensure_default_status_rules,
    save_status_rules_from_form,
)
from app.client_notify_history import booking_notify_timeline
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    BookingStatusNotifyRule,
    Client,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserRole,
)
from app.db.session import get_db
from app.main import app


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _make(db, *, when: datetime, chat: int | None = 501, status=BookingStatus.PENDING_CONFIRMATION, no_promo=False):
    user = User(
        username=f"u{chat or 0}{int(when.timestamp())}",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(
        name="Катя",
        is_confirmed=True,
        telegram_chat_id=chat,
        no_promo=no_promo,
    )
    db.add_all([user, client])
    db.flush()
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=when,
        kind=BookingKind.VISIT,
        status=status,
    )
    db.add(booking)
    db.commit()
    return booking


def _rows(db, booking_id: int) -> list[NotificationOutbox]:
    return list(
        db.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.booking_id == int(booking_id),
                NotificationOutbox.event_type == EVENT_CLIENT_BOOKING_STATUS,
            )
            .order_by(NotificationOutbox.id.asc())
        ).all()
    )


def test_default_rule_and_send_once_until_datetime_changes() -> None:
    db = _session()
    future = datetime(2026, 12, 1, 8, 0)
    booking = _make(db, when=future)
    assert _rows(db, booking.id) == []
    assert ensure_default_status_rules(db) is True
    db.commit()
    rule = db.scalar(select(BookingStatusNotifyRule))
    assert rule is not None
    assert rule.status == BookingStatus.ACTIVE.value
    assert rule.is_enabled is True
    assert rule.attach_memo_before is True
    assert rule.template == DEFAULT_CONFIRMED_TEMPLATE

    booking.status = BookingStatus.ACTIVE
    db.commit()
    sent = [row for row in _rows(db, booking.id) if row.status == NotificationOutboxStatus.PENDING]
    assert len(sent) == 1
    assert "Катя" in (sent[0].body or "")
    assert "подтверждена" in (sent[0].body or "")
    assert "{{name}}" not in (sent[0].body or "")

    booking.status = BookingStatus.PENDING_CONFIRMATION
    db.commit()
    booking.status = BookingStatus.ACTIVE
    db.commit()
    sent = [row for row in _rows(db, booking.id) if row.status == NotificationOutboxStatus.PENDING]
    assert len(sent) == 1
    skips = [row for row in _rows(db, booking.id) if row.error == SKIP_ALREADY]
    assert len(skips) == 1

    booking.planned_date = datetime(2026, 12, 2, 8, 0)
    booking.status = BookingStatus.PENDING_CONFIRMATION
    db.commit()
    booking.status = BookingStatus.ACTIVE
    db.commit()
    sent = [row for row in _rows(db, booking.id) if row.status == NotificationOutboxStatus.PENDING]
    assert len(sent) == 2


def test_skips_past_disabled_and_missing_channel() -> None:
    db = _session()
    ensure_default_status_rules(db)
    db.commit()
    past = _make(db, when=datetime(2020, 1, 1, 8, 0), chat=11)
    past.status = BookingStatus.ACTIVE
    db.commit()
    assert [row.error for row in _rows(db, past.id)] == [SKIP_PAST]

    future = _make(db, when=datetime(2026, 12, 3, 8, 0), chat=12)
    rule = db.scalar(select(BookingStatusNotifyRule))
    assert rule is not None
    rule.is_enabled = False
    db.commit()
    future.status = BookingStatus.ACTIVE
    db.commit()
    assert [row.error for row in _rows(db, future.id)] == [SKIP_DISABLED]

    rule.is_enabled = True
    db.commit()
    silent = _make(db, when=datetime(2026, 12, 4, 8, 0), chat=None)
    silent.status = BookingStatus.ACTIVE
    db.commit()
    assert [row.error for row in _rows(db, silent.id)] == [SKIP_NO_CHANNEL]


def test_no_promo_does_not_block_and_history_labels_skip() -> None:
    db = _session()
    ensure_default_status_rules(db)
    db.commit()
    booking = _make(db, when=datetime(2026, 12, 5, 8, 0), chat=77, no_promo=True)
    booking.status = BookingStatus.ACTIVE
    db.commit()
    pending = [row for row in _rows(db, booking.id) if row.status == NotificationOutboxStatus.PENDING]
    assert len(pending) == 1
    booking.status = BookingStatus.CANCELLED
    db.commit()
    booking.status = BookingStatus.ACTIVE
    db.commit()
    items = booking_notify_timeline(db, booking.id, tz_name="Asia/Novosibirsk")
    labels = [item["type_label"] for item in items if item["kind"] == "out"]
    assert "Информация о брони: ✔️ подтверждена" in labels
    skip = next(item for item in items if item["status_label"] == SKIP_ALREADY)
    assert skip["channel_label"] == "—"


def test_memo_before_attached_from_booking(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.booking_status_notify.care_memo_ids_for_booking",
        lambda db, booking: [7],
    )
    monkeypatch.setattr(
        "app.booking_status_notify.memo_names",
        lambda db, ids: ["До визита"],
    )
    db = _session()
    ensure_default_status_rules(db)
    db.commit()
    booking = _make(db, when=datetime(2026, 12, 6, 8, 0), chat=90)
    booking.status = BookingStatus.ACTIVE
    db.commit()
    row = _rows(db, booking.id)[0]
    import json

    payload = json.loads(row.payload_json)
    assert payload["care_memo_ids"] == [7]
    assert payload["care_memos_attached"] == ["До визита"]

    rule = db.scalar(select(BookingStatusNotifyRule))
    assert rule is not None
    rule.attach_memo_before = False
    db.commit()
    booking.planned_date = datetime(2026, 12, 7, 8, 0)
    booking.status = BookingStatus.PENDING_CONFIRMATION
    db.commit()
    booking.status = BookingStatus.ACTIVE
    db.commit()
    pending = [item for item in _rows(db, booking.id) if item.status == NotificationOutboxStatus.PENDING]
    last = json.loads(pending[-1].payload_json)
    assert "care_memo_ids" not in last


def test_duplicate_status_rejected() -> None:
    db = _session()
    ensure_default_status_rules(db)
    from app.booking_status_notify import add_status_rule

    add_status_rule(db, BookingStatus.CANCELLED.value)
    db.commit()
    rows = list(db.scalars(select(BookingStatusNotifyRule).order_by(BookingStatusNotifyRule.id)).all())
    with pytest.raises(ValueError, match="только в одном правиле"):
        save_status_rules_from_form(
            db,
            ids=[row.id for row in rows],
            statuses=[BookingStatus.ACTIVE.value, BookingStatus.ACTIVE.value],
            templates=[row.template for row in rows],
            enabled_flags=["1", "1"],
            memo_flags=["1", "1"],
        )


def test_notifications_page_splits_before_block() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.commit()
    auth = AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name,
        role=UserRole.ADMIN,
        roles=(UserRole.ADMIN,),
    )

    def _user():
        return auth

    def _db():
        yield db

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    try:
        html = TestClient(app).get("/clients/notifications").text
    finally:
        app.dependency_overrides.clear()
    assert "Информация о брони" in html
    assert "Напоминания" in html
    assert "После визита" in html
    assert "Прикрепить памятку" in html
    assert "Ваша запись подтверждена" in html
    assert 'name="before_hours"' in html
    assert "Добавить правило" in html
    assert "Добавить напоминание" in html
