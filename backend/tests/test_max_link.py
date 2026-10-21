"""3.15: Max webhook — bot_started, занятый max_user_id, enqueue канала max."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStatus,
    Client,
    ClientNotificationSend,
    NotificationChannel,
    NotificationOutbox,
    TelegramLinkToken,
    User,
    UserRole,
    VisitMastersScope,
)
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.messenger_inbound import MSG_CONFIRMED, handle_client_text
from app.notifications import enqueue_master_booking_notifications, send_max
from app.time_utils import utcnow_naive
from app.notify_prefs import apply_notify_prefs_from_legacy_flag
from app.routes.max_webhook import max_webhook
from app.settings import get_settings
from app.telegram_link import (
    MSG_CLIENT_MAX_TAKEN,
    MSG_LINKED_OK,
    MSG_MAX_TAKEN,
    ClientChannelTakenError,
    MaxUserTakenError,
    bind_client_max,
    bind_client_telegram,
    bind_client_vk,
    bind_max_user,
    bind_telegram_chat,
    bind_vk_user,
    create_max_link_token,
    max_deep_link,
)


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


def _seed_user(db, *, username: str = "master_max", max_user_id: int | None = None) -> User:
    u = User(
        username=username,
        password_hash="x",
        display_name="Мастер Max",
        role=UserRole.MASTER,
        is_active=True,
        notify_enabled=True,
        max_user_id=max_user_id,
    )
    db.add(u)
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [UserRole.MASTER], enabled=True)
    db.commit()
    db.refresh(u)
    return u


def _run_max(db, payload: dict, *, secret: str | None = "sec"):
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)
    headers = {"X-Max-Bot-Api-Secret": secret} if secret is not None else {}

    async def _call():
        return await max_webhook(
            req,
            db=db,
            x_max_bot_api_secret=headers.get("X-Max-Bot-Api-Secret"),
        )

    return asyncio.run(_call())


def test_max_deep_link(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_USERNAME", "lb_bot")
    get_settings.cache_clear()
    assert max_deep_link("abc") == "https://max.ru/lb_bot?start=abc"


def test_wrong_secret_403(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "expected")
    get_settings.cache_clear()
    res = _run_max(memory_db, {"update_type": "bot_started", "payload": "x"}, secret="wrong")
    assert res.status_code == 403


def test_missing_secret_env_403(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    res = _run_max(memory_db, {"update_type": "bot_started"}, secret=None)
    assert res.status_code == 403


def test_bot_started_binds(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_max_link_token(memory_db, u.id)
    memory_db.commit()
    sent: list[tuple[int, str]] = []

    def _fake(uid: int, text: str) -> None:
        sent.append((uid, text))

    with patch("app.routes.max_webhook.send_max", side_effect=_fake):
        res = _run_max(
            memory_db,
            {
                "update_type": "bot_started",
                "payload": plain,
                "user": {"user_id": 900001},
                "chat_id": 55,
            },
        )
    assert res.status_code == 200
    memory_db.refresh(u)
    assert u.max_user_id == 900001
    assert sent == [(900001, MSG_LINKED_OK)]
    tok = memory_db.scalar(select(TelegramLinkToken).where(TelegramLinkToken.user_id == u.id))
    assert tok is not None and tok.used_at is not None and tok.channel == "max"


def test_bot_started_taken_rejects(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    owner = _seed_user(memory_db, username="owner", max_user_id=424242)
    other = _seed_user(memory_db, username="other")
    plain, _ = create_max_link_token(memory_db, other.id)
    memory_db.commit()
    sent: list[str] = []

    def _fake(uid: int, text: str) -> None:
        sent.append(text)

    with patch("app.routes.max_webhook.send_max", side_effect=_fake):
        res = _run_max(
            memory_db,
            {
                "update_type": "bot_started",
                "payload": plain,
                "user": {"user_id": 424242},
            },
        )
    assert res.status_code == 200
    memory_db.refresh(other)
    memory_db.refresh(owner)
    assert other.max_user_id is None
    assert owner.max_user_id == 424242
    assert MSG_MAX_TAKEN in sent


def test_bind_max_user_taken_raises(memory_db) -> None:
    a = _seed_user(memory_db, username="a", max_user_id=1)
    b = _seed_user(memory_db, username="b")
    with pytest.raises(MaxUserTakenError):
        bind_max_user(memory_db, b, 1)
    memory_db.refresh(a)
    assert a.max_user_id == 1


def test_message_created_start_binds(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_max_link_token(memory_db, u.id)
    memory_db.commit()
    with patch("app.routes.max_webhook.send_max"):
        res = _run_max(
            memory_db,
            {
                "update_type": "message_created",
                "message": {
                    "sender": {"user_id": 77},
                    "body": {"text": f"/start {plain}"},
                },
            },
        )
    assert res.status_code == 200
    memory_db.refresh(u)
    assert u.max_user_id == 77


def test_enqueue_creates_max_channel(memory_db) -> None:
    u = _seed_user(memory_db, max_user_id=555)
    c = Client(name="Клиент", phone="+79990001111", is_confirmed=True)
    memory_db.add(c)
    memory_db.commit()
    memory_db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 10, 5, 10, 0, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    memory_db.add(b)
    memory_db.commit()
    memory_db.refresh(b)
    memory_db.add(BookingMaster(booking_id=b.id, master_id=u.id))
    memory_db.commit()
    rows = enqueue_master_booking_notifications(memory_db, b, "booking_created")
    memory_db.commit()
    assert len(rows) == 1
    assert rows[0].channel == NotificationChannel.MAX
    assert memory_db.scalar(select(NotificationOutbox.id).limit(1)) is not None


def test_send_max_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Max не настроен"):
        send_max(1, "hi")


def test_send_max_mocked(monkeypatch) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    seen: dict[str, object] = {}

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            seen["verify"] = kwargs.get("verify")

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def request(self, method: str, url: str, content: bytes | None = None, headers: dict | None = None):
            seen["url"] = url
            seen["headers"] = headers
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b'{"message":{"mid":"1"}}'
            return resp

    with patch("app.max_http.httpx.Client", _Client):
        send_max(42, "Тест Живём Плетём")
    assert "user_id=42" in str(seen["url"])
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "tok"
    assert seen["verify"] is not False
    assert isinstance(seen["verify"], str) and seen["verify"]


def test_staff_and_client_may_share_max_and_reply_finds_booking(memory_db) -> None:
    shared = 424242
    staff = _seed_user(memory_db, max_user_id=shared)
    client = Client(name="Катя", phone="+79990001122", is_confirmed=True)
    memory_db.add(client)
    memory_db.commit()
    memory_db.refresh(client)

    bind_client_max(memory_db, client, shared)
    memory_db.commit()
    memory_db.refresh(staff)
    memory_db.refresh(client)
    assert staff.max_user_id == shared
    assert client.max_user_id == shared

    planned = utcnow_naive() + timedelta(hours=48)
    booking = Booking(
        created_by_user_id=staff.id,
        client_id=client.id,
        planned_date=planned,
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    memory_db.add(booking)
    memory_db.commit()
    memory_db.refresh(booking)
    memory_db.add(
        ClientNotificationSend(
            client_id=client.id,
            booking_id=booking.id,
            channel="max",
            event_type=EVENT_CLIENT_BOOKING_REMINDER,
            target_id=shared,
            sent_at=utcnow_naive(),
        )
    )
    memory_db.commit()

    assert handle_client_text(memory_db, channel="max", messenger_id=shared, text="1") == MSG_CONFIRMED
    memory_db.refresh(booking)
    assert booking.client_confirmed_at is not None
    assert booking.client_confirmed_via == "max"

    memory_db.add(BookingMaster(booking_id=booking.id, master_id=staff.id))
    memory_db.commit()
    rows = enqueue_master_booking_notifications(memory_db, booking, "booking_created")
    assert len(rows) == 1
    assert rows[0].channel == NotificationChannel.MAX
    assert int(rows[0].user_id) == staff.id


def test_shared_account_without_booking_is_not_a_client_reply(memory_db) -> None:
    shared = 515151
    _seed_user(memory_db, username="quiet", max_user_id=shared)
    client = Client(name="Оля", phone="+79990001133", is_confirmed=True, max_user_id=shared)
    memory_db.add(client)
    memory_db.commit()
    assert handle_client_text(memory_db, channel="max", messenger_id=shared, text="1") is None


def test_two_clients_cannot_share_max(memory_db) -> None:
    first = Client(name="Аня", is_confirmed=True, max_user_id=777)
    second = Client(name="Белла", is_confirmed=True)
    memory_db.add_all([first, second])
    memory_db.commit()
    with pytest.raises(ClientChannelTakenError, match="другому клиенту") as raised:
        bind_client_max(memory_db, second, 777)
    assert str(raised.value) == MSG_CLIENT_MAX_TAKEN
    memory_db.refresh(first)
    assert first.max_user_id == 777
    assert second.max_user_id is None


def test_staff_may_share_vk_and_telegram_with_client(memory_db) -> None:
    staff = _seed_user(memory_db, username="channels")
    staff.vk_user_id = 111
    staff.telegram_chat_id = 222
    client = Client(name="Катя", is_confirmed=True)
    memory_db.add(client)
    memory_db.commit()
    bind_client_vk(memory_db, client, 111)
    bind_client_telegram(memory_db, client, 222)
    memory_db.commit()
    memory_db.refresh(staff)
    memory_db.refresh(client)
    assert staff.vk_user_id == 111 and client.vk_user_id == 111
    assert staff.telegram_chat_id == 222 and client.telegram_chat_id == 222

    other = Client(name="Другая", is_confirmed=True)
    memory_db.add(other)
    memory_db.commit()
    with pytest.raises(ClientChannelTakenError, match="другому клиенту"):
        bind_client_vk(memory_db, other, 111)
    with pytest.raises(ClientChannelTakenError, match="другому клиенту"):
        bind_client_telegram(memory_db, other, 222)

    bind_vk_user(memory_db, staff, 111)
    bind_telegram_chat(memory_db, staff, 222)
    memory_db.refresh(client)
    assert client.vk_user_id == 111
    assert client.telegram_chat_id == 222


def test_two_staff_cannot_share_max(memory_db) -> None:
    _seed_user(memory_db, username="owner_x", max_user_id=9)
    other = _seed_user(memory_db, username="other_x")
    with pytest.raises(MaxUserTakenError, match="другой сотруднице"):
        bind_max_user(memory_db, other, 9)
    memory_db.refresh(other)
    assert other.max_user_id is None
