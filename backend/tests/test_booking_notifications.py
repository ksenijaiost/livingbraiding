"""3.4: уведомления мастерам при create/update/cancel брони."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from app.booking_notifications import (
    notify_booking_cancelled,
    notify_booking_created,
    notify_booking_updated_with_master_diff,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStatus,
    Client,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserRole,
    VisitMastersScope,
)
from app.notifications import EVENT_BOOKING_CANCELLED, EVENT_BOOKING_CREATED, EVENT_BOOKING_UPDATED
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed(db, *, tg: int | None = 555, notify: bool = True, planned_delta_hours: int = 48, username: str = "m1"):
    u = User(
        username=username,
        password_hash="x",
        display_name="Мастер",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=tg,
        notify_enabled=notify,
    )
    c = Client(name="Клиент", phone=f"+7999{abs(hash(username)) % 10000000:07d}", is_confirmed=True)
    db.add_all([u, c])
    db.commit()
    db.refresh(u)
    db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=utcnow_naive() + timedelta(hours=planned_delta_hours),
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        comment="тест",
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


def test_create_enqueues_and_sends(memory_db) -> None:
    u, b = _seed(memory_db)
    sent: list[tuple[int, str]] = []

    def _fake(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    with patch("app.notifications.send_telegram", side_effect=_fake):
        notify_booking_created(memory_db, int(b.id))

    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    assert len(rows) == 1
    assert rows[0].event_type == EVENT_BOOKING_CREATED
    assert rows[0].status == NotificationOutboxStatus.SENT
    assert rows[0].user_id == u.id
    assert len(sent) == 1
    assert "создана" in sent[0][1]


def test_noop_resave_no_duplicate_updated(memory_db) -> None:
    u, b = _seed(memory_db)
    mids = [u.id]
    with patch("app.notifications.send_telegram"):
        notify_booking_updated_with_master_diff(memory_db, int(b.id), old_master_ids=mids)
        n1 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
        notify_booking_updated_with_master_diff(memory_db, int(b.id), old_master_ids=mids)
        n2 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
    assert n1 == 1
    assert n2 == 1


def test_date_change_sends_new_updated(memory_db) -> None:
    u, b = _seed(memory_db)
    mids = [u.id]
    with patch("app.notifications.send_telegram"):
        notify_booking_updated_with_master_diff(memory_db, int(b.id), old_master_ids=mids)
        n1 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
        b.planned_date = b.planned_date + timedelta(days=1)
        b.updated_at = utcnow_naive()
        memory_db.commit()
        notify_booking_updated_with_master_diff(memory_db, int(b.id), old_master_ids=mids)
        n2 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
    assert n2 == n1 + 1
    kinds = {r.event_type for r in memory_db.scalars(select(NotificationOutbox)).all()}
    assert EVENT_BOOKING_UPDATED in kinds


def test_cancel_notifies(memory_db) -> None:
    _, b = _seed(memory_db)
    b.status = BookingStatus.CANCELLED
    b.cancelled_at = utcnow_naive()
    memory_db.commit()
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_cancelled(memory_db, int(b.id))
    row = memory_db.scalars(select(NotificationOutbox)).first()
    assert row is not None
    assert row.event_type == EVENT_BOOKING_CANCELLED
    assert mock_send.called


def test_send_error_does_not_break_flow(memory_db) -> None:
    _, b = _seed(memory_db)

    def _boom(chat_id: int, text: str) -> None:
        raise RuntimeError("telegram down")

    with patch("app.notifications.send_telegram", side_effect=_boom):
        notify_booking_created(memory_db, int(b.id))
    row = memory_db.scalars(select(NotificationOutbox)).first()
    assert row is not None
    assert row.status == NotificationOutboxStatus.FAILED
    assert "telegram down" in (row.error or "")


def test_no_chat_or_notify_disabled_skips(memory_db) -> None:
    _, b1 = _seed(memory_db, tg=None, notify=True, username="m_no_tg")
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_created(memory_db, int(b1.id))
    assert (
        memory_db.scalar(
            select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.booking_id == b1.id)
        )
        == 0
    )
    assert not mock_send.called

    _, b2 = _seed(memory_db, tg=999, notify=False, username="m_off")
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_created(memory_db, int(b2.id))
    rows = list(
        memory_db.scalars(select(NotificationOutbox).where(NotificationOutbox.booking_id == b2.id)).all()
    )
    assert rows == []
    assert not mock_send.called


def test_past_booking_skipped(memory_db) -> None:
    _, b = _seed(memory_db, planned_delta_hours=-5)
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_created(memory_db, int(b.id))
    assert memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    assert not mock_send.called


def test_master_added_gets_created_removed_gets_unassign(memory_db) -> None:
    u1, b = _seed(memory_db, tg=111, username="m_a")
    u2 = User(
        username="m_b",
        password_hash="x",
        display_name="Мастер2",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=222,
        notify_enabled=True,
    )
    memory_db.add(u2)
    memory_db.commit()
    memory_db.refresh(u2)

    memory_db.execute(delete(BookingMaster).where(BookingMaster.booking_id == b.id))
    memory_db.add(BookingMaster(booking_id=b.id, master_id=u2.id))
    b.updated_at = utcnow_naive()
    memory_db.commit()

    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_updated_with_master_diff(memory_db, int(b.id), old_master_ids=[u1.id])

    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    by_user = {r.user_id: r for r in rows}
    assert by_user[u2.id].event_type == EVENT_BOOKING_CREATED
    assert by_user[u1.id].event_type == EVENT_BOOKING_CANCELLED
    texts = " ".join(call.args[1] for call in mock_send.call_args_list)
    assert "снята с вас" in texts
