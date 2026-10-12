"""3.23: сообщение после визита — задержка от сохранения и «не позже»."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.client_after_visit import (
    after_visit_scheduled_at,
    cancel_scheduled_after_visit,
    enqueue_due_after_booking_deadlines,
    schedule_after_visit_notifications,
)
from app.client_notifications import KIND_AFTER, parse_latest_time, parse_rule_hours
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    ClientNotificationRule,
    NotificationOutbox,
    NotificationOutboxStatus,
    Setting,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitMastersScope,
    VisitPriceType,
)
from app.notifications import process_outbox
from app.setting_keys import DISPLAY_TIMEZONE

TZ = "Asia/Novosibirsk"
DAY = datetime(2026, 10, 12)


def _utc(local: datetime) -> datetime:
    return local.replace(tzinfo=ZoneInfo(TZ)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


@pytest.fixture()
def db():
    session = memory_db()
    try:
        yield session
    finally:
        session.close()


def _tz(db) -> None:
    db.add(Setting(key=DISPLAY_TIMEZONE, value=TZ, updated_at=datetime(2026, 1, 1)))
    db.flush()


def _people(db, *, vk: int = 1001):
    user = User(
        username=f"adm{vk}",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(name="Катя", is_confirmed=True, vk_user_id=vk)
    db.add_all([user, client])
    db.flush()
    return user, client


def _rule(db, *, hours: float, latest: time | None, template: str = "Спасибо, {{name}}") -> ClientNotificationRule:
    row = ClientNotificationRule(
        kind=KIND_AFTER,
        hours=hours,
        latest_time=latest,
        template=template,
        position=0,
        is_enabled=True,
        created_at=datetime(2026, 1, 1),
    )
    db.add(row)
    db.flush()
    return row


def _booking(db, user, client, *, when_local: datetime, status=BookingStatus.ACTIVE) -> Booking:
    row = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=_utc(when_local),
        kind=BookingKind.VISIT,
        status=status,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(row)
    db.flush()
    return row


def _visit(db, client, *, saved_local: datetime, booking_id: int | None = None, cancelled: bool = False) -> Visit:
    row = Visit(
        created_at=_utc(saved_local),
        performed_date=datetime.combine(saved_local.date(), datetime.min.time()),
        client_id=client.id,
        booking_id=booking_id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        masters_scope=VisitMastersScope.VISIT,
        is_cancelled=cancelled,
    )
    db.add(row)
    db.flush()
    return row


def _after_rows(db) -> list[NotificationOutbox]:
    return list(db.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id)).all())


def test_parse_delay_and_latest() -> None:
    assert parse_rule_hours("2.5", kind=KIND_AFTER) == 2.5
    assert parse_rule_hours("2,5", kind=KIND_AFTER) == 2.5
    assert parse_rule_hours(0, kind=KIND_AFTER) == 0
    assert parse_rule_hours(72, kind=KIND_AFTER) == 72
    with pytest.raises(ValueError):
        parse_rule_hours("2.25", kind=KIND_AFTER)
    with pytest.raises(ValueError):
        parse_rule_hours(80, kind=KIND_AFTER)
    assert parse_latest_time("20:00") == time(20, 0)
    assert parse_latest_time("") is None
    with pytest.raises(ValueError):
        parse_latest_time("25:00")


def test_saved_at_examples() -> None:
    latest = time(20, 0)
    day = DAY
    saved_15 = _utc(datetime(2026, 10, 12, 15, 0))
    assert after_visit_scheduled_at(
        saved_at=saved_15,
        performed_date=day,
        delay_hours=2,
        latest_time=latest,
        tz_name=TZ,
    ) == _utc(datetime(2026, 10, 12, 17, 0))
    saved_19 = _utc(datetime(2026, 10, 12, 19, 0))
    assert after_visit_scheduled_at(
        saved_at=saved_19,
        performed_date=day,
        delay_hours=2,
        latest_time=latest,
        tz_name=TZ,
    ) == _utc(datetime(2026, 10, 12, 20, 0))
    saved_21 = _utc(datetime(2026, 10, 12, 21, 0))
    assert after_visit_scheduled_at(
        saved_at=saved_21,
        performed_date=day,
        delay_hours=2,
        latest_time=latest,
        tz_name=TZ,
    ) == saved_21
    assert after_visit_scheduled_at(
        saved_at=saved_15,
        performed_date=day,
        delay_hours=0,
        latest_time=latest,
        tz_name=TZ,
    ) == saved_15


def test_schedule_writes_planned_time_and_does_not_duplicate(db) -> None:
    _tz(db)
    _, client = _people(db)
    _rule(db, hours=2, latest=time(20, 0))
    visit = _visit(db, client, saved_local=datetime(2026, 10, 12, 15, 0))
    rows = schedule_after_visit_notifications(db, visit)
    assert len(rows) == 1
    assert rows[0].scheduled_at == _utc(datetime(2026, 10, 12, 17, 0))
    assert rows[0].visit_id == visit.id
    assert rows[0].status == NotificationOutboxStatus.PENDING
    assert "Катя" in rows[0].payload_json
    again = schedule_after_visit_notifications(db, visit)
    assert again == []
    assert len(_after_rows(db)) == 1


def test_deadline_by_booking_then_visit_does_not_repeat(db) -> None:
    _tz(db)
    user, client = _people(db)
    _rule(db, hours=2, latest=time(20, 0))
    booking = _booking(db, user, client, when_local=datetime(2026, 10, 12, 12, 0))
    now = _utc(datetime(2026, 10, 12, 20, 5))
    stats = enqueue_due_after_booking_deadlines(db, now=now)
    assert stats["enqueued"] == 1
    row = _after_rows(db)[0]
    assert row.booking_id == booking.id
    assert row.visit_id is None
    assert row.scheduled_at == _utc(datetime(2026, 10, 12, 20, 0))
    visit = _visit(db, client, saved_local=datetime(2026, 10, 12, 21, 0), booking_id=booking.id)
    assert schedule_after_visit_notifications(db, visit) == []
    assert len(_after_rows(db)) == 1


def test_without_latest_time_booking_sends_nothing(db) -> None:
    _tz(db)
    user, client = _people(db)
    _rule(db, hours=2, latest=None)
    _booking(db, user, client, when_local=datetime(2026, 10, 12, 12, 0))
    stats = enqueue_due_after_booking_deadlines(db, now=_utc(datetime(2026, 10, 12, 20, 5)))
    assert stats["enqueued"] == 0
    assert _after_rows(db) == []


def test_cancelled_booking_and_cancelled_visit(db, monkeypatch) -> None:
    _tz(db)
    user, client = _people(db)
    _rule(db, hours=2, latest=time(20, 0))
    _booking(
        db,
        user,
        client,
        when_local=datetime(2026, 10, 12, 12, 0),
        status=BookingStatus.CANCELLED,
    )
    stats = enqueue_due_after_booking_deadlines(db, now=_utc(datetime(2026, 10, 12, 20, 5)))
    assert stats["enqueued"] == 0

    _, client2 = _people(db, vk=4040)
    visit = _visit(db, client2, saved_local=datetime(2026, 10, 12, 15, 0))
    assert len(schedule_after_visit_notifications(db, visit)) == 1
    assert cancel_scheduled_after_visit(db, visit) == 1
    sent: list[int] = []
    monkeypatch.setattr("app.notifications._send_outbox_row", lambda row: sent.append(row.id))
    process_outbox(db, limit=10)
    assert sent == []
    row = _after_rows(db)[0]
    assert row.status == NotificationOutboxStatus.CANCELLED


def test_future_scheduled_is_not_sent_yet(db, monkeypatch) -> None:
    _tz(db)
    _, client = _people(db)
    _rule(db, hours=2, latest=None)
    saved = datetime.utcnow() + timedelta(hours=1)
    visit = Visit(
        created_at=saved,
        performed_date=datetime.combine(saved.date(), datetime.min.time()),
        client_id=client.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        masters_scope=VisitMastersScope.VISIT,
    )
    db.add(visit)
    db.flush()
    rows = schedule_after_visit_notifications(db, visit)
    assert len(rows) == 1
    assert rows[0].scheduled_at is not None
    assert rows[0].scheduled_at > datetime.utcnow()
    sent: list[int] = []
    monkeypatch.setattr("app.notifications._send_outbox_row", lambda row: sent.append(row.id))
    process_outbox(db, limit=10)
    assert sent == []
    assert rows[0].status == NotificationOutboxStatus.PENDING


def test_visit_saved_before_deadline_suppresses_booking_send(db) -> None:
    _tz(db)
    user, client = _people(db)
    _rule(db, hours=2, latest=time(20, 0))
    booking = _booking(db, user, client, when_local=datetime(2026, 10, 12, 12, 0))
    visit = _visit(db, client, saved_local=datetime(2026, 10, 12, 15, 0), booking_id=booking.id)
    created = schedule_after_visit_notifications(db, visit)
    assert len(created) == 1
    assert created[0].scheduled_at == _utc(datetime(2026, 10, 12, 17, 0))
    stats = enqueue_due_after_booking_deadlines(db, now=_utc(datetime(2026, 10, 12, 20, 5)))
    assert stats["enqueued"] == 0
    assert db.scalar(select(func.count()).select_from(NotificationOutbox)) == 1
