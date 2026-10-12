"""3.7: фоновый воркер notification_outbox."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
from app.notification_worker import (
    notification_worker_loop,
    run_outbox_tick,
    start_notification_worker,
)
from app.notifications import enqueue_master_booking_notifications, process_outbox
from app.settings import get_settings


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


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _seed(db, *, tg: int = 555):
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username="m1",
        password_hash="x",
        display_name="Мастер",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=tg,
        notify_enabled=True,
    )
    c = Client(name="Клиент", phone="+79990001111", is_confirmed=True)
    db.add_all([u, c])
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [UserRole.MASTER], enabled=True)
    db.commit()
    db.refresh(u)
    db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 10, 5, 10, 0, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    db.add(BookingMaster(booking_id=b.id, master_id=u.id))
    db.commit()
    db.refresh(b)
    return u, b


def test_worker_tick_processes_pending_and_failed(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    _, b = _seed(memory_db)
    rows = enqueue_master_booking_notifications(
        memory_db, b, "booking_created", channels={NotificationChannel.TELEGRAM}
    )
    memory_db.commit()
    assert len(rows) == 1
    row_id = int(rows[0].id)

    with patch("app.notifications.send_telegram", side_effect=RuntimeError("down")):
        process_outbox(memory_db, limit=10, max_attempts=5)
    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.FAILED

    SessionLocal = sessionmaker(bind=memory_db.get_bind())

    class _CM:
        def __enter__(self):
            self.db = SessionLocal()
            return self.db

        def __exit__(self, *a):
            self.db.close()

    with patch("app.notification_worker.SessionLocal", _CM):
        with patch("app.notifications.send_telegram", return_value=None):
            stats = run_outbox_tick()
    assert stats["sent"] == 1
    memory_db.expire_all()
    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.SENT


def test_worker_tick_skips_sent(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    _, b = _seed(memory_db)
    rows = enqueue_master_booking_notifications(
        memory_db, b, "booking_created", channels={NotificationChannel.TELEGRAM}
    )
    memory_db.commit()
    row_id = int(rows[0].id)

    SessionLocal = sessionmaker(bind=memory_db.get_bind())

    class _CM:
        def __enter__(self):
            self.db = SessionLocal()
            return self.db

        def __exit__(self, *a):
            self.db.close()

    with patch("app.notification_worker.SessionLocal", _CM):
        with patch("app.notifications.send_telegram", return_value=None) as mock_tg:
            assert run_outbox_tick()["sent"] == 1
            assert mock_tg.call_count == 1
            assert run_outbox_tick()["processed"] == 0
            assert mock_tg.call_count == 1

    memory_db.expire_all()
    row = memory_db.get(NotificationOutbox, row_id)
    assert row is not None
    assert row.status == NotificationOutboxStatus.SENT


def test_worker_loop_survives_send_error(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFICATION_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    stop = asyncio.Event()
    calls = {"n": 0}

    def _tick():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick boom")
        stop.set()
        return {"processed": 0, "sent": 0, "failed": 0}

    async def _run():
        with patch("app.notification_worker.run_outbox_tick", side_effect=_tick):
            await notification_worker_loop(stop, interval_seconds=0.01)
        assert calls["n"] >= 2

    asyncio.run(_run())


def test_start_worker_disabled(monkeypatch) -> None:
    monkeypatch.setenv("NOTIFICATION_WORKER_ENABLED", "false")
    monkeypatch.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    assert start_notification_worker() is None


def test_claim_does_not_double_send_fresh_sending(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    _, b = _seed(memory_db)
    rows = enqueue_master_booking_notifications(
        memory_db, b, "booking_created", channels={NotificationChannel.TELEGRAM}
    )
    memory_db.commit()
    row = rows[0]
    row.status = NotificationOutboxStatus.SENDING
    row.locked_at = datetime(2099, 1, 1, 0, 0, 0)
    memory_db.commit()

    with patch("app.notifications.send_telegram", return_value=None) as mock_tg:
        stats = process_outbox(memory_db, limit=10, max_attempts=5)
    assert stats["processed"] == 0
    mock_tg.assert_not_called()
    memory_db.refresh(row)
    assert row.status == NotificationOutboxStatus.SENDING
