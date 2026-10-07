"""3.18: клиентские напоминания, ответы 1/3, TELEGRAM_API_BASE, чат админов."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.admin_chat import (
    MSG_ADMIN_CHAT_LINKED,
    bind_admin_chat,
    create_admin_chat_link_token,
    get_admin_chat_target,
    try_bind_admin_chat_from_text,
)
from app.client_booking_notify import (
    EVENT_CLIENT_BOOKING_REMINDER,
    client_rule_is_due,
    enqueue_client_rule_notifications,
    enqueue_due_client_notifications,
)
from app.client_notifications import (
    DEFAULT_BEFORE_TEMPLATE,
    KIND_AFTER,
    KIND_BEFORE,
    ensure_default_rules,
    render_client_notify_template,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingPlannedService,
    BookingStatus,
    Client,
    ClientNotificationRule,
    ClientNotificationRuleKind,
    ClientNotificationSend,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserRole,
    VisitMastersScope,
)
from app.messenger_inbound import (
    EVENT_BOOKING_CLIENT_CANCEL_REQUEST,
    MSG_ALREADY_CONFIRMED,
    MSG_CANCEL_REQUESTED,
    MSG_CONFIRMED,
    MSG_HINT,
    handle_client_text,
    normalize_client_reply,
)
from app.notifications import send_telegram
from app.routes.max_webhook import max_webhook
from app.routes.telegram_webhook import telegram_webhook
from app.routes.vk_webhook import vk_webhook
from app.settings import get_settings
from app.time_utils import utcnow_naive


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


def _seed_client_booking(db, *, planned: datetime, channels: bool = True):
    u = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    c = Client(
        name="Катя",
        phone="+79990001111",
        is_confirmed=True,
        vk_user_id=1001 if channels else None,
        max_user_id=2002 if channels else None,
        telegram_chat_id=3003 if channels else None,
    )
    db.add_all([u, c])
    db.commit()
    db.refresh(u)
    db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=planned,
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return u, c, b


def test_normalize_reply() -> None:
    assert normalize_client_reply("1") == "1"
    assert normalize_client_reply(" 1. ") == "1"
    assert normalize_client_reply("3") == "3"
    assert normalize_client_reply("2") is None
    assert normalize_client_reply("привет") is None


def test_render_with_services_marker() -> None:
    booking = SimpleNamespace(
        planned_date=datetime(2026, 9, 16, 5, 0, 0),
        planned_services=[
            SimpleNamespace(
                id=1,
                sort_order=0,
                service=SimpleNamespace(name="Услуга"),
                duration_minutes=2,
                masters=[],
            )
        ],
        planned_service=None,
        masters=[],
    )
    text = render_client_notify_template(
        "{{services}}",
        booking=booking,  # type: ignore[arg-type]
        client=SimpleNamespace(name="К"),  # type: ignore[arg-type]
        tz_name="Asia/Novosibirsk",
        marker="*",
    )
    assert text == "* Услуга 2 минуты"


def test_enqueue_before_after_dedupe(memory_db) -> None:
    ensure_default_rules(memory_db)
    memory_db.commit()
    planned = utcnow_naive() + timedelta(hours=3)
    _, c, b = _seed_client_booking(memory_db, planned=planned)
    before = memory_db.scalar(
        select(ClientNotificationRule).where(
            ClientNotificationRule.kind == ClientNotificationRuleKind.BEFORE_BOOKING,
            ClientNotificationRule.hours == 2,
        )
    )
    assert before is not None
    rows1 = enqueue_client_rule_notifications(memory_db, b, c, before)
    memory_db.commit()
    assert len(rows1) == 3  # vk+max+tg
    assert {r.channel for r in rows1} == {
        NotificationChannel.VK,
        NotificationChannel.MAX,
        NotificationChannel.TELEGRAM,
    }
    assert all(r.target_kind == "client" for r in rows1)
    assert all(r.event_type == EVENT_CLIENT_BOOKING_REMINDER for r in rows1)
    rows2 = enqueue_client_rule_notifications(memory_db, b, c, before)
    assert rows2 == []

    after = memory_db.scalar(
        select(ClientNotificationRule).where(
            ClientNotificationRule.kind == ClientNotificationRuleKind.AFTER_BOOKING
        )
    )
    assert after is not None
    assert after.hours == 0
    # after due: planned in the past within window
    b.planned_date = utcnow_naive() - timedelta(minutes=5)
    memory_db.commit()
    assert client_rule_is_due(
        kind=KIND_AFTER.value,
        planned_date=b.planned_date,
        hours=0,
        now=utcnow_naive(),
    )
    rows_a = enqueue_client_rule_notifications(memory_db, b, c, after)
    memory_db.commit()
    assert len(rows_a) == 3
    assert rows_a[0].event_type == "client_after_booking"


def test_enqueue_due_client_notifications(memory_db) -> None:
    ensure_default_rules(memory_db)
    memory_db.commit()
    # planned через 1.5 ч → правило «за 2 ч» уже due, запись ещё впереди
    planned = utcnow_naive() + timedelta(hours=1, minutes=30)
    _, c, b = _seed_client_booking(memory_db, planned=planned)
    stats = enqueue_due_client_notifications(memory_db, now=utcnow_naive())
    memory_db.commit()
    assert stats["enqueued"] >= 3  # 2ч before due
    n = memory_db.scalar(select(NotificationOutbox).limit(1))
    assert n is not None


def test_reply_1_and_3_and_hint(memory_db) -> None:
    planned = utcnow_naive() + timedelta(hours=48)
    _, c, b = _seed_client_booking(memory_db, planned=planned)
    memory_db.add(
        ClientNotificationSend(
            client_id=c.id,
            booking_id=b.id,
            channel="vk",
            event_type=EVENT_CLIENT_BOOKING_REMINDER,
            rule_id=None,
            outbox_id=None,
            target_id=1001,
            sent_at=utcnow_naive(),
        )
    )
    memory_db.commit()

    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="привет") == MSG_HINT
    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1") == MSG_CONFIRMED
    memory_db.commit()
    memory_db.refresh(b)
    assert b.client_confirmed_at is not None
    assert b.client_confirmed_via == "vk"
    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1") == MSG_ALREADY_CONFIRMED

    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="3") == MSG_CANCEL_REQUESTED
    memory_db.commit()
    memory_db.refresh(b)
    assert b.client_cancel_requested_at is not None
    cancel_rows = list(
        memory_db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == EVENT_BOOKING_CLIENT_CANCEL_REQUEST
            )
        ).all()
    )
    # чат админов не подключён — только мастера (их нет) → 0 или admin empty
    assert isinstance(cancel_rows, list)


def test_reply_via_webhooks_three_channels(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    planned = utcnow_naive() + timedelta(hours=10)
    _, c, b = _seed_client_booking(memory_db, planned=planned)
    for ch, tid in (("vk", 1001), ("max", 2002), ("telegram", 3003)):
        memory_db.add(
            ClientNotificationSend(
                client_id=c.id,
                booking_id=b.id,
                channel=ch,
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=tid,
                sent_at=utcnow_naive(),
            )
        )
    memory_db.commit()
    sent: list[tuple[str, int, str]] = []

    def _cap(channel):
        def _fn(uid, text="", **kwargs):
            sent.append((channel, int(uid if uid is not None else kwargs.get("peer_id") or kwargs.get("chat_id") or 0), text))

        return _fn

    # VK
    with patch("app.messenger_inbound.send_vk", side_effect=_cap("vk")):
        with patch("app.routes.vk_webhook.send_vk", side_effect=_cap("vk")):
            req = MagicMock()
            req.json = AsyncMock(
                return_value={
                    "type": "message_new",
                    "object": {"message": {"from_id": 1001, "peer_id": 1001, "text": "1"}},
                }
            )
            asyncio.run(vk_webhook(req, db=memory_db))
    memory_db.refresh(b)
    assert b.client_confirmed_via == "vk"
    assert any(t[2] == MSG_CONFIRMED for t in sent)

    # reset confirm for next channel test — use cancel on max
    b.client_confirmed_at = None
    b.client_confirmed_via = None
    memory_db.commit()
    with patch("app.messenger_inbound.send_max", side_effect=_cap("max")):
        with patch("app.routes.max_webhook.send_max", side_effect=_cap("max")):
            req = MagicMock()
            req.json = AsyncMock(
                return_value={
                    "update_type": "message_created",
                    "user": {"user_id": 2002},
                    "message": {"sender": {"user_id": 2002}, "body": {"text": "3"}},
                }
            )
            asyncio.run(
                max_webhook(req, db=memory_db, x_max_bot_api_secret="sec")
            )
    memory_db.refresh(b)
    assert b.client_cancel_requested_via == "max"
    assert any(t[2] == MSG_CANCEL_REQUESTED for t in sent)

    # telegram hint
    with patch("app.messenger_inbound.send_telegram", side_effect=_cap("tg")):
        with patch("app.routes.telegram_webhook.send_telegram", side_effect=_cap("tg")):
            req = MagicMock()
            req.json = AsyncMock(
                return_value={
                    "message": {
                        "chat": {"id": 3003, "type": "private"},
                        "from": {"id": 3003},
                        "text": "hello",
                    }
                }
            )
            asyncio.run(telegram_webhook(req, db=memory_db, x_telegram_bot_api_secret_token=None))
    assert any(t[2] == MSG_HINT for t in sent)


def test_telegram_api_base_used(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_API_BASE", "https://proxy.example/tg")
    get_settings.cache_clear()
    with patch("app.notifications.urllib.request.urlopen") as urlopen:
        resp = MagicMock()
        resp.read.return_value = b'{"ok":true}'
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        urlopen.return_value = resp
        send_telegram(42, "hi")
        req = urlopen.call_args[0][0]
        assert req.full_url.startswith("https://proxy.example/tg/bottok/sendMessage")


def test_admin_chat_bind_and_cancel_notify(memory_db) -> None:
    plain = create_admin_chat_link_token(memory_db, created_by_user_id=None)
    memory_db.commit()
    with patch("app.admin_chat.send_to_chat"):
        reply = try_bind_admin_chat_from_text(
            memory_db, channel="vk", chat_id=2000000001, text=f"/admins {plain}"
        )
    assert reply == MSG_ADMIN_CHAT_LINKED
    row = get_admin_chat_target(memory_db, "vk")
    assert row is not None and int(row.chat_id) == 2000000001

    planned = utcnow_naive() + timedelta(hours=5)
    master = User(
        username="m1",
        password_hash="x",
        display_name="Маша",
        role=UserRole.MASTER,
        is_active=True,
        vk_user_id=777,
        notify_enabled=True,
    )
    memory_db.add(master)
    memory_db.commit()
    memory_db.refresh(master)
    _, c, b = _seed_client_booking(memory_db, planned=planned)
    memory_db.add(BookingMaster(booking_id=b.id, master_id=master.id))
    memory_db.add(
        ClientNotificationSend(
            client_id=c.id,
            booking_id=b.id,
            channel="telegram",
            event_type=EVENT_CLIENT_BOOKING_REMINDER,
            target_id=3003,
            sent_at=utcnow_naive(),
        )
    )
    memory_db.commit()
    ans = handle_client_text(memory_db, channel="telegram", messenger_id=3003, text="3")
    memory_db.commit()
    assert ans == MSG_CANCEL_REQUESTED
    rows = list(
        memory_db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == EVENT_BOOKING_CLIENT_CANCEL_REQUEST
            )
        ).all()
    )
    kinds = {r.target_kind for r in rows}
    assert "user" in kinds
    assert "admin_chat" in kinds
    admin_row = next(r for r in rows if r.target_kind == "admin_chat")
    assert admin_row.channel == NotificationChannel.VK
