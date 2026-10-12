"""3.24: история уведомлений и ответов клиента в брони и в карточке."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.client_booking_notify import EVENT_CLIENT_AFTER_BOOKING
from app.client_notify_history import booking_notify_timeline, client_recent_messages
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    ClientInboundMessage,
    ClientNotificationRule,
    ClientNotificationRuleKind,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitMastersScope,
    VisitPriceType,
)
from app.routes.max_webhook import max_webhook
from app.routes.telegram_webhook import telegram_webhook
from app.routes.vk_webhook import vk_webhook
from app.settings import get_settings

TZ = "UTC"


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        yield db


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _user(db, *, username: str, vk: int | None = None) -> User:
    row = User(
        username=username,
        password_hash="x",
        display_name="Анна",
        role=UserRole.ADMIN,
        is_active=True,
        vk_user_id=vk,
    )
    db.add(row)
    db.flush()
    return row


def _client(db) -> Client:
    row = Client(
        name="Катя",
        is_confirmed=True,
        vk_user_id=1001,
        max_user_id=2002,
        telegram_chat_id=3003,
    )
    db.add(row)
    db.flush()
    return row


def _booking(db, user: User, client: Client) -> Booking:
    row = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 12, 8, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(row)
    db.flush()
    return row


def _out(db, **kwargs) -> NotificationOutbox:
    row = NotificationOutbox(
        user_id=kwargs.get("user_id"),
        client_id=kwargs.get("client_id"),
        target_kind=kwargs.get("target_kind", "client"),
        booking_id=kwargs.get("booking_id"),
        visit_id=kwargs.get("visit_id"),
        channel=kwargs.get("channel", NotificationChannel.VK),
        event_type=kwargs["event"],
        payload_json='{"text":"из payload"}',
        status=kwargs["status"],
        error=kwargs.get("error"),
        dedupe_key=kwargs["key"],
        attempt_count=kwargs.get("attempts", 0),
        created_at=kwargs["when"],
        scheduled_at=kwargs.get("scheduled_at"),
        sent_at=kwargs.get("sent_at"),
        body=kwargs.get("body", "текст"),
        rule_id=kwargs.get("rule_id"),
    )
    db.add(row)
    db.flush()
    return row


def test_booking_timeline_statuses_and_after_visit(memory_db) -> None:
    user = _user(memory_db, username="adm")
    client = _client(memory_db)
    booking = _booking(memory_db, user, client)
    rule = ClientNotificationRule(
        kind=ClientNotificationRuleKind.BEFORE_BOOKING,
        hours=24,
        template="напомни",
        position=0,
        is_enabled=True,
        created_at=datetime(2026, 1, 1),
    )
    memory_db.add(rule)
    memory_db.flush()
    visit = Visit(
        created_at=datetime(2026, 10, 12, 10, 0),
        performed_date=datetime(2026, 10, 12),
        client_id=client.id,
        booking_id=booking.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        masters_scope=VisitMastersScope.VISIT,
    )
    memory_db.add(visit)
    memory_db.flush()
    _out(
        memory_db,
        key="plan",
        event="client_booking_reminder",
        status=NotificationOutboxStatus.PENDING,
        when=datetime(2026, 10, 12, 6, 0),
        scheduled_at=datetime(2026, 10, 12, 7, 0),
        booking_id=booking.id,
        client_id=client.id,
        body="завтра",
        rule_id=rule.id,
    )
    _out(
        memory_db,
        key="sent",
        event="client_booking_reminder",
        status=NotificationOutboxStatus.SENT,
        when=datetime(2026, 10, 12, 7, 0),
        sent_at=datetime(2026, 10, 12, 7, 5),
        booking_id=booking.id,
        client_id=client.id,
        body="ушло",
        rule_id=rule.id,
        channel=NotificationChannel.TELEGRAM,
    )
    _out(
        memory_db,
        key="fail",
        event="client_booking_reminder",
        status=NotificationOutboxStatus.FAILED,
        when=datetime(2026, 10, 12, 8, 0),
        booking_id=booking.id,
        client_id=client.id,
        body="не ушло",
        error="сеть",
        attempts=3,
        channel=NotificationChannel.MAX,
    )
    _out(
        memory_db,
        key="after",
        event=EVENT_CLIENT_AFTER_BOOKING,
        status=NotificationOutboxStatus.SENT,
        when=datetime(2026, 10, 12, 13, 0),
        sent_at=datetime(2026, 10, 12, 13, 0),
        visit_id=visit.id,
        client_id=client.id,
        body="спасибо",
    )
    _out(
        memory_db,
        key="master",
        event="booking_client_cancel_request",
        status=NotificationOutboxStatus.SENT,
        when=datetime(2026, 10, 12, 9, 0),
        sent_at=datetime(2026, 10, 12, 9, 0),
        booking_id=booking.id,
        user_id=user.id,
        target_kind="user",
        body="просит отменить",
    )
    memory_db.add(
        ClientInboundMessage(
            client_id=client.id,
            booking_id=None,
            channel="vk",
            external_user_id=1001,
            text="4",
            parsed_action="contact_admin",
            received_at=datetime(2026, 10, 12, 11, 0),
        )
    )
    memory_db.add(
        ClientInboundMessage(
            client_id=client.id,
            booking_id=booking.id,
            channel="vk",
            external_user_id=1001,
            text="1",
            parsed_action="confirm",
            received_at=datetime(2026, 10, 12, 7, 10),
        )
    )
    memory_db.flush()

    items = booking_notify_timeline(memory_db, booking.id, tz_name=TZ)
    labels = [item["status_label"] for item in items if item["kind"] == "out"]
    assert any(item.startswith("запланировано") for item in labels)
    assert any(item.startswith("отправлено") for item in labels)
    assert any("ошибка: сеть" in item and "попыток: 3" in item for item in labels)
    assert any(item["type_label"] == "Напоминание за 24 ч" for item in items)
    assert any(item["type_label"] == "После визита" and "спасибо" in item["text"] for item in items)
    assert any(item["mark"] == "мастеру" and item["destination"].startswith("мастеру") for item in items)
    assert any(item["kind"] == "in" and item["parsed_label"] == "1 — подтвердила" for item in items)
    assert all(item["parsed_label"] != "4 — просит связаться" for item in items)
    assert [item["at"] for item in items] == sorted(item["at"] for item in items)

    recent = client_recent_messages(memory_db, client.id, tz_name=TZ)
    assert any(item["parsed_label"] == "4 — просит связаться" and item["booking_id"] is None for item in recent)
    assert any(item["booking_id"] == booking.id for item in recent)


def _fire(db, channel: str, text: str, *, vk_from: int = 1001, peer: int = 1001) -> None:
    def _cap(uid=None, text="", **kwargs):
        return None

    if channel == "vk":
        req = MagicMock()
        req.json = AsyncMock(
            return_value={
                "type": "message_new",
                "object": {"message": {"from_id": vk_from, "peer_id": peer, "text": text}},
            }
        )
        with patch("app.messenger_inbound.send_vk", side_effect=_cap), patch(
            "app.routes.vk_webhook.send_vk", side_effect=_cap
        ):
            asyncio.run(vk_webhook(req, db=db))
    elif channel == "max":
        req = MagicMock()
        req.json = AsyncMock(
            return_value={
                "update_type": "message_created",
                "user": {"user_id": 2002},
                "message": {"sender": {"user_id": 2002}, "body": {"text": text}},
            }
        )
        with patch("app.messenger_inbound.send_max", side_effect=_cap), patch(
            "app.routes.max_webhook.send_max", side_effect=_cap
        ):
            asyncio.run(max_webhook(req, db=db, x_max_bot_api_secret="sec"))
    else:
        req = MagicMock()
        req.json = AsyncMock(
            return_value={
                "message": {
                    "chat": {"id": 3003, "type": "private"},
                    "from": {"id": 3003},
                    "text": text,
                }
            }
        )
        with patch("app.messenger_inbound.send_telegram", side_effect=_cap), patch(
            "app.routes.telegram_webhook.send_telegram", side_effect=_cap
        ):
            asyncio.run(telegram_webhook(req, db=db, x_telegram_bot_api_secret_token=None))


def test_inbound_logged_on_each_channel(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    user = _user(memory_db, username="adm")
    client = _client(memory_db)
    booking = _booking(memory_db, user, client)
    _user(memory_db, username="staff", vk=7777)
    memory_db.commit()

    for channel in ("vk", "max", "telegram"):
        _fire(memory_db, channel, "5")
    _fire(memory_db, "vk", "1", vk_from=424242)
    _fire(memory_db, "vk", "1", vk_from=7777)
    _fire(memory_db, "vk", "1", vk_from=50, peer=2_000_000_001)

    rows = list(memory_db.scalars(select(ClientInboundMessage).order_by(ClientInboundMessage.id)).all())
    by_channel = [row for row in rows if row.client_id == client.id and row.text == "5"]
    assert {row.channel for row in by_channel} == {"vk", "max", "telegram"}
    assert all(row.parsed_action == "directions" and row.booking_id is None for row in by_channel)
    unknown = [row for row in rows if row.external_user_id == 424242]
    assert len(unknown) == 1 and unknown[0].client_id is None and unknown[0].parsed_action == "unknown"
    assert all(row.external_user_id != 7777 for row in rows)
    assert all(row.external_user_id != 50 for row in rows)

    assert booking_notify_timeline(memory_db, booking.id, tz_name=TZ) == []
    recent = client_recent_messages(memory_db, client.id, tz_name=TZ)
    assert any(item["parsed_label"] == "5 — как добраться" for item in recent)
    assert all(item["booking_id"] is None for item in recent if item["kind"] == "in")
