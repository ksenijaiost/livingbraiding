"""3.11: настраиваемые напоминания о предстоящих бронях."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.booking_reminders import (
    DEFAULT_REMINDER_MINUTES,
    EVENT_BOOKING_REMINDER,
    MAX_REMINDERS_PER_USER,
    add_user_reminder_hours,
    clear_user_reminders,
    enqueue_due_booking_reminders,
    get_effective_reminder_minutes,
    parse_reminder_hours,
    reminder_is_due,
    replace_user_reminder_minutes,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStatus,
    Client,
    NotificationChannel,
    NotificationOutbox,
    User,
    UserRole,
    VisitMastersScope,
)
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_master(db, *, username: str = "m1", tg: int | None = 100, vk: int | None = None, notify: bool = True):
    u = User(
        username=username,
        password_hash="x",
        display_name=f"Мастер {username}",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=tg,
        vk_user_id=vk,
        notify_enabled=notify,
        reminders_configured=False,
    )
    c = Client(name="Клиент", phone=f"+7999{abs(hash(username)) % 10000000:07d}", is_confirmed=True)
    db.add_all([u, c])
    db.commit()
    db.refresh(u)
    db.refresh(c)
    return u, c


def _seed_booking(db, user: User, client: Client, *, planned: datetime, status=BookingStatus.ACTIVE):
    b = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=planned,
        kind=BookingKind.VISIT,
        status=status,
        comment="коммент",
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    db.add(BookingMaster(booking_id=b.id, master_id=user.id))
    db.commit()
    db.refresh(b)
    return b


def test_default_reminders_24_and_2(memory_db) -> None:
    u, _ = _seed_master(memory_db)
    assert get_effective_reminder_minutes(memory_db, u) == list(DEFAULT_REMINDER_MINUTES)


def test_parse_and_limit_validation(memory_db) -> None:
    u, _ = _seed_master(memory_db)
    with pytest.raises(ValueError):
        parse_reminder_hours(0.1)
    with pytest.raises(ValueError):
        parse_reminder_hours(100)
    replace_user_reminder_minutes(memory_db, u, [60, 120, 180, 360, 1440])
    memory_db.commit()
    with pytest.raises(ValueError, match="Не больше"):
        add_user_reminder_hours(memory_db, u, 4)
    with pytest.raises(ValueError, match="уже есть"):
        add_user_reminder_hours(memory_db, u, 1)
    clear_user_reminders(memory_db, u)
    memory_db.commit()
    assert get_effective_reminder_minutes(memory_db, u) == []
    assert u.reminders_configured is True


def test_masters_settings_independent(memory_db) -> None:
    a, _ = _seed_master(memory_db, username="a", tg=1)
    b, _ = _seed_master(memory_db, username="b", tg=2)
    replace_user_reminder_minutes(memory_db, a, [60])
    replace_user_reminder_minutes(memory_db, b, [120, 240])
    memory_db.commit()
    assert get_effective_reminder_minutes(memory_db, a) == [60]
    assert get_effective_reminder_minutes(memory_db, b) == [120, 240]


def test_reminder_due_window() -> None:
    now = datetime(2026, 10, 5, 12, 0, 0)
    planned = now + timedelta(hours=2)
    assert reminder_is_due(planned_date=planned, minutes_before=120, now=now)
    assert not reminder_is_due(planned_date=planned, minutes_before=120, now=now - timedelta(minutes=1))
    assert not reminder_is_due(
        planned_date=planned,
        minutes_before=120,
        now=now + timedelta(minutes=11),
    )
    assert not reminder_is_due(planned_date=now - timedelta(minutes=1), minutes_before=120, now=now)


def test_enqueue_once_at_due_time(memory_db) -> None:
    u, c = _seed_master(memory_db, tg=555, vk=777)
    replace_user_reminder_minutes(memory_db, u, [120])
    memory_db.commit()
    now = utcnow_naive()
    planned = now + timedelta(hours=2)
    b = _seed_booking(memory_db, u, c, planned=planned)

    stats1 = enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert stats1["enqueued"] == 2  # tg + vk
    n1 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
    assert n1 == 2
    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    assert all(r.event_type == EVENT_BOOKING_REMINDER for r in rows)
    assert {r.channel for r in rows} == {NotificationChannel.TELEGRAM, NotificationChannel.VK}
    text = rows[0].payload_json
    assert "Напоминание: запись через 2 ч" in text
    assert "Клиент" in text

    stats2 = enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert stats2["enqueued"] == 0
    n2 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
    assert n2 == 2


def test_reschedule_recalculates_dedupe(memory_db) -> None:
    u, c = _seed_master(memory_db, tg=555)
    replace_user_reminder_minutes(memory_db, u, [60])
    memory_db.commit()
    now = utcnow_naive()
    b = _seed_booking(memory_db, u, c, planned=now + timedelta(hours=1))
    enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0) == 1

    b.planned_date = now + timedelta(hours=1, minutes=30)
    memory_db.commit()
    # Старое due уже не подходит; новое fire_at = planned-60m = now+30m — ещё рано.
    enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0) == 1

    later = now + timedelta(minutes=30)
    enqueue_due_booking_reminders(memory_db, now=later)
    memory_db.commit()
    assert int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0) == 2


def test_cancel_blocks_reminders(memory_db) -> None:
    u, c = _seed_master(memory_db, tg=555)
    replace_user_reminder_minutes(memory_db, u, [120])
    memory_db.commit()
    now = utcnow_naive()
    b = _seed_booking(memory_db, u, c, planned=now + timedelta(hours=2), status=BookingStatus.CANCELLED)
    stats = enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert stats["enqueued"] == 0
    assert int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0) == 0


def test_removed_master_no_reminder(memory_db) -> None:
    u, c = _seed_master(memory_db, tg=555)
    u2, _ = _seed_master(memory_db, username="m2", tg=556)
    replace_user_reminder_minutes(memory_db, u, [120])
    memory_db.commit()
    now = utcnow_naive()
    b = _seed_booking(memory_db, u2, c, planned=now + timedelta(hours=2))
    # u не назначен
    enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert (
        memory_db.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(NotificationOutbox.user_id == u.id)
        )
        == 0
    )


def test_stale_reminder_not_sent(memory_db) -> None:
    u, c = _seed_master(memory_db, tg=555)
    replace_user_reminder_minutes(memory_db, u, [120])
    memory_db.commit()
    now = utcnow_naive()
    # fire_at = planned - 2h; if planned = now+2h-15min, fire was 15 min ago → stale
    planned = now + timedelta(hours=2) - timedelta(minutes=15)
    _seed_booking(memory_db, u, c, planned=planned)
    stats = enqueue_due_booking_reminders(memory_db, now=now)
    memory_db.commit()
    assert stats["enqueued"] == 0


def test_notify_disabled_or_no_channel_skips(memory_db) -> None:
    u, c = _seed_master(memory_db, tg=None, notify=True)
    replace_user_reminder_minutes(memory_db, u, [120])
    memory_db.commit()
    now = utcnow_naive()
    _seed_booking(memory_db, u, c, planned=now + timedelta(hours=2))
    assert enqueue_due_booking_reminders(memory_db, now=now)["enqueued"] == 0

    u2, c2 = _seed_master(memory_db, username="off", tg=9, notify=False)
    replace_user_reminder_minutes(memory_db, u2, [120])
    memory_db.commit()
    _seed_booking(memory_db, u2, c2, planned=now + timedelta(hours=2))
    assert enqueue_due_booking_reminders(memory_db, now=now)["enqueued"] == 0


def test_max_five_constant() -> None:
    assert MAX_REMINDERS_PER_USER == 5
