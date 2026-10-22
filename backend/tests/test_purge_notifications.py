"""3.46: удаление брони и клиента отвязывает уведомления, а не стирает их."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.client_notify_history import client_recent_messages, outbox_subject_flags
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    ClientInboundMessage,
    ClientNotificationSend,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserRole,
)
from app.notifications import process_outbox
from app.super_admin_purge import build_purge_preview, purge_booking_hard, purge_client_hard
from app.time_utils import utcnow_naive


def _db():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)()


def _seed(db, *, username: str, name: str):
    user = User(
        username=username,
        password_hash="x",
        display_name="Юля",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    client = Client(name=name, is_confirmed=True)
    db.add_all([user, client])
    db.flush()
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 21, 12, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
    )
    db.add(booking)
    db.flush()
    now = utcnow_naive()
    pending = NotificationOutbox(
        client_id=client.id,
        target_kind="client",
        booking_id=booking.id,
        channel=NotificationChannel.VK,
        event_type="client_booking_reminder",
        payload_json='{"text":"напомнить","booking_id":%d,"target_kind":"client"}' % int(booking.id),
        body="напомнить",
        status=NotificationOutboxStatus.PENDING,
        dedupe_key=f"pend-{username}",
        created_at=now,
    )
    sent = NotificationOutbox(
        client_id=client.id,
        target_kind="client",
        booking_id=booking.id,
        channel=NotificationChannel.VK,
        event_type="client_booking_status",
        payload_json='{"text":"статус","booking_id":%d,"target_kind":"client"}' % int(booking.id),
        body="статус",
        status=NotificationOutboxStatus.SENT,
        dedupe_key=f"sent-{username}",
        sent_at=now,
        created_at=now,
    )
    db.add_all([pending, sent])
    db.flush()
    db.add(
        ClientNotificationSend(
            client_id=client.id,
            booking_id=booking.id,
            channel="vk",
            event_type="client_booking_status",
            outbox_id=sent.id,
            target_id=1001,
            sent_at=now,
        )
    )
    db.add(
        ClientInboundMessage(
            client_id=client.id,
            booking_id=booking.id,
            channel="vk",
            external_user_id=1001,
            text="1",
            parsed_action="confirm",
            received_at=now,
        )
    )
    db.commit()
    return client, booking, pending, sent


def test_purge_booking_and_client_unlink_notifications() -> None:
    db = _db()
    client, booking, pending, sent = _seed(db, username="yulya", name="Катя")
    other, other_booking, other_pending, other_sent = _seed(db, username="admin", name="Оля")
    bid = int(booking.id)
    cid = int(client.id)

    preview = build_purge_preview(db, "booking", bid)
    assert preview["ok"]
    assert any("Уведомления будут отвязаны, не удалены: 4" in line for line in preview["lines"])

    purge_booking_hard(db, bid, actor_user_id=None)
    db.commit()

    assert db.get(Booking, bid) is None
    assert db.get(Client, cid) is not None
    db.refresh(pending)
    db.refresh(sent)
    assert pending.booking_id is None
    assert sent.booking_id is None
    assert pending.client_id == cid
    assert pending.status == NotificationOutboxStatus.CANCELLED
    assert sent.status == NotificationOutboxStatus.SENT
    sends = list(db.scalars(select(ClientNotificationSend).where(ClientNotificationSend.client_id == cid)).all())
    inbound = list(db.scalars(select(ClientInboundMessage).where(ClientInboundMessage.client_id == cid)).all())
    assert len(sends) == 1 and sends[0].booking_id is None
    assert len(inbound) == 1 and inbound[0].booking_id is None
    flags = outbox_subject_flags(pending)
    assert flags["booking_removed"] is True
    assert flags["client_removed"] is False
    recent = client_recent_messages(db, cid, tz_name="UTC", limit=10)
    assert any(item.get("booking_removed") for item in recent)

    stats = process_outbox(db, limit=20, only_ids=[int(pending.id)])
    assert stats["processed"] == 0
    db.refresh(pending)
    assert pending.status == NotificationOutboxStatus.CANCELLED

    other_preview = build_purge_preview(db, "client", int(other.id))
    assert any("отвязаны, не удалены: 4" in line for line in other_preview["lines"])
    other_id = int(other.id)
    other_bid = int(other_booking.id)
    purge_client_hard(db, other_id, actor_user_id=None)
    db.commit()
    assert db.get(Client, other_id) is None
    assert db.get(Booking, other_bid) is None
    db.refresh(other_pending)
    db.refresh(other_sent)
    assert other_pending.booking_id is None and other_pending.client_id is None
    assert other_sent.booking_id is None and other_sent.client_id is None
    assert other_pending.status == NotificationOutboxStatus.CANCELLED
    assert other_sent.status == NotificationOutboxStatus.SENT
    left_sends = list(db.scalars(select(ClientNotificationSend)).all())
    left_in = list(db.scalars(select(ClientInboundMessage)).all())
    assert any(row.client_id is None and row.booking_id is None for row in left_sends)
    assert any(row.client_id is None and row.booking_id is None for row in left_in)
    assert outbox_subject_flags(other_pending)["client_removed"] is True
    assert outbox_subject_flags(other_pending)["booking_removed"] is True
