"""3.2: модуль уведомлений мастерам (текст, outbox, TG/VK)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

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
    NotificationOutboxStatus,
    User,
    UserRole,
    VisitMastersScope,
)
from app.notifications import (
    EVENT_BOOKING_CREATED,
    EVENT_BOOKING_UPDATED,
    build_booking_master_message,
    enqueue_master_booking_notifications,
    process_outbox,
    send_telegram,
    send_vk,
)
from app.settings import get_settings


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed(db, *, tg: int | None = 111, vk: int | None = None, notify: bool = True):
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username="m1",
        password_hash="x",
        display_name="Мастер Аня",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=tg,
        vk_user_id=vk,
        notify_enabled=notify,
    )
    c = Client(name="Клиент Катя", phone="+79990001111", is_confirmed=True)
    db.add_all([u, c])
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [UserRole.MASTER], enabled=notify)
    db.commit()
    db.refresh(u)
    db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 10, 5, 10, 0, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        comment="Принести фото",
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    db.add(BookingMaster(booking_id=b.id, master_id=u.id))
    db.commit()
    db.refresh(b)
    return u, c, b


def test_build_booking_master_message_ru(memory_db) -> None:
    _, _, b = _seed(memory_db)
    b = memory_db.get(Booking, b.id)
    assert b is not None
    text = build_booking_master_message(b, EVENT_BOOKING_CREATED, tz_name="UTC")
    assert f"Бронь #{b.id} создана" in text
    assert "Клиент: Клиент Катя" in text
    assert "Тип: Визит" in text
    assert "Принести фото" in text
    assert "05.10.2026 10:00" in text


def test_enqueue_skips_without_masters(memory_db) -> None:
    u, c, _ = _seed(memory_db)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 10, 6, 12, 0, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    memory_db.add(b)
    memory_db.commit()
    rows = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    assert rows == []


def test_enqueue_skips_notify_disabled(memory_db) -> None:
    _, _, b = _seed(memory_db, notify=False)
    rows = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    assert rows == []


def test_enqueue_creates_channels_and_dedupes(memory_db) -> None:
    _, _, b = _seed(memory_db, tg=555, vk=777)
    rows1 = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()
    assert len(rows1) == 2
    channels = {r.channel for r in rows1}
    assert channels == {NotificationChannel.TELEGRAM, NotificationChannel.VK}

    rows2 = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()
    assert rows2 == []
    n = memory_db.scalar(select(func.count()).select_from(NotificationOutbox))
    assert int(n or 0) == 2


def test_enqueue_updated_new_version(memory_db) -> None:
    _, _, b = _seed(memory_db, tg=555, vk=None)
    enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()
    b.updated_at = datetime(2026, 10, 5, 11, 0, 0)
    memory_db.commit()
    rows = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_UPDATED)
    memory_db.commit()
    assert len(rows) == 1
    assert rows[0].event_type == EVENT_BOOKING_UPDATED


def test_send_vk_without_token(monkeypatch) -> None:
    monkeypatch.setenv("VK_GROUP_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="VK не настроен"):
        send_vk(1, "привет")


def test_send_telegram_without_token(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        send_telegram(1, "привет")


def test_process_outbox_sent_and_failed(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("VK_GROUP_TOKEN", "")
    get_settings.cache_clear()
    _, _, b = _seed(memory_db, tg=555, vk=999)
    enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()

    def _tg_ok(chat_id, text):
        return None

    with patch("app.notifications.send_telegram", side_effect=_tg_ok):
        stats = process_outbox(memory_db, limit=10, max_attempts=3)
        memory_db.commit()

    assert stats["processed"] == 2
    assert stats["sent"] == 1
    assert stats["failed"] == 1

    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    by_ch = {r.channel: r for r in rows}
    assert by_ch[NotificationChannel.TELEGRAM].status == NotificationOutboxStatus.SENT
    assert by_ch[NotificationChannel.VK].status == NotificationOutboxStatus.FAILED
    assert "VK не настроен" in (by_ch[NotificationChannel.VK].error or "")
    assert by_ch[NotificationChannel.VK].attempt_count == 1


def test_process_outbox_retry_delivers_failed(memory_db, monkeypatch) -> None:
    """Ретрай успешно доставляет ранее failed запись."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    _, _, b = _seed(memory_db, tg=555, vk=None)
    rows = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()
    assert len(rows) == 1
    row_id = int(rows[0].id)

    with patch("app.notifications.send_telegram", side_effect=RuntimeError("сеть")):
        stats1 = process_outbox(memory_db, limit=10, max_attempts=5)
        memory_db.commit()
    assert stats1["failed"] == 1
    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.FAILED
    assert row.attempt_count == 1

    with patch("app.notifications.send_telegram", return_value=None):
        stats2 = process_outbox(memory_db, limit=10, max_attempts=5)
        memory_db.commit()
    assert stats2["processed"] == 1
    assert stats2["sent"] == 1
    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.SENT
    assert row.attempt_count == 2
    assert row.error is None
    assert row.sent_at is not None


def test_process_outbox_stops_after_max_attempts(memory_db, monkeypatch) -> None:
    """После лимита попыток failed больше не берётся в обработку."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    _, _, b = _seed(memory_db, tg=555, vk=None)
    rows = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()
    row_id = int(rows[0].id)
    max_att = 3

    with patch("app.notifications.send_telegram", side_effect=RuntimeError("down")):
        for _ in range(max_att):
            process_outbox(memory_db, limit=10, max_attempts=max_att)
            memory_db.commit()

    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.FAILED
    assert row.attempt_count == max_att

    with patch("app.notifications.send_telegram", return_value=None) as mock_tg:
        stats = process_outbox(memory_db, limit=10, max_attempts=max_att)
        memory_db.commit()
    assert stats == {"processed": 0, "sent": 0, "failed": 0}
    mock_tg.assert_not_called()
    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.attempt_count == max_att
    assert row.status == NotificationOutboxStatus.FAILED


def test_process_outbox_does_not_resend_sent(memory_db, monkeypatch) -> None:
    """Повторный запуск не дублирует уже sent."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    _, _, b = _seed(memory_db, tg=555, vk=None)
    rows = enqueue_master_booking_notifications(memory_db, b, EVENT_BOOKING_CREATED)
    memory_db.commit()
    row_id = int(rows[0].id)

    with patch("app.notifications.send_telegram", return_value=None) as mock_tg:
        stats1 = process_outbox(memory_db, limit=10, max_attempts=5)
        memory_db.commit()
        assert stats1["sent"] == 1
        assert mock_tg.call_count == 1

        stats2 = process_outbox(memory_db, limit=10, max_attempts=5)
        memory_db.commit()
        assert stats2 == {"processed": 0, "sent": 0, "failed": 0}
        assert mock_tg.call_count == 1

    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.SENT
    assert row.attempt_count == 1
