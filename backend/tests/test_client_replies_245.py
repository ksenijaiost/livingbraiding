"""3.22: ответы клиента 2/4/5, тексты бота, «Обработано»."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.booking_status_notify import EVENT_CLIENT_BOOKING_STATUS
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.client_bot_replies import render_bot_reply, vk_community_dialog_url
from app.client_notifications import render_client_notify_template
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    AdminChatTarget,
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    ClientNotificationSend,
    NotificationOutbox,
    StudioTemplateVariable,
    User,
    UserRole,
    VisitMastersScope,
)
from app.messenger_inbound import (
    EVENT_BOOKING_CLIENT_RESCHEDULE_REQUEST,
    EVENT_CLIENT_CONTACT_REQUEST,
    MSG_RESCHEDULE_REQUESTED,
    handle_client_text,
    mark_client_reply_handled,
)
from app.routes.max_webhook import max_webhook
from app.routes.telegram_webhook import telegram_webhook
from app.routes.vk_webhook import vk_webhook
from app.settings import get_settings
from app.studio_template_variables import save_builtin_values, ensure_builtin_studio_variables
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


def _seed(db, *, planned: datetime, with_send: bool = True):
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(
        name="Катя",
        phone="+79990001111",
        is_confirmed=True,
        vk_user_id=1001,
        max_user_id=2002,
        telegram_chat_id=3003,
    )
    db.add_all([user, client])
    db.commit()
    db.refresh(user)
    db.refresh(client)
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=planned,
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    if with_send:
        db.add(
            ClientNotificationSend(
                client_id=client.id,
                booking_id=booking.id,
                channel="vk",
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=1001,
                sent_at=utcnow_naive(),
            )
        )
        db.commit()
    db.add(
        AdminChatTarget(channel="vk", chat_id=2000000001, title="admins", linked_at=utcnow_naive())
    )
    db.commit()
    return user, client, booking


def _fire_webhook(db, channel: str, text: str, sent: list[str] | None = None) -> None:
    def _cap(uid=None, text="", **kwargs):
        if sent is not None:
            sent.append(text)

    if channel == "vk":
        req = MagicMock()
        req.json = AsyncMock(
            return_value={
                "type": "message_new",
                "object": {"message": {"from_id": 1001, "peer_id": 1001, "text": text}},
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


def test_replies_2_4_5_via_webhooks(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "")
    monkeypatch.setenv("VK_GROUP_ID", "555")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    planned = utcnow_naive() + timedelta(hours=20)
    user, client, booking = _seed(memory_db, planned=planned)

    for channel in ("vk", "max", "telegram"):
        _fire_webhook(memory_db, channel, "2")
        memory_db.refresh(booking)
        assert booking.client_reschedule_requested_via == channel
    reschedule_n = memory_db.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.event_type == EVENT_BOOKING_CLIENT_RESCHEDULE_REQUEST)
    )
    assert int(reschedule_n or 0) == 1

    mark_client_reply_handled(memory_db, booking, "reschedule", user_id=user.id)
    memory_db.commit()
    memory_db.refresh(booking)
    assert booking.client_reschedule_requested_at is None
    assert booking.client_reschedule_handled_at is not None
    assert booking.client_reschedule_handled_by_user_id == user.id

    _fire_webhook(memory_db, "vk", "2")
    reschedule_n = memory_db.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.event_type == EVENT_BOOKING_CLIENT_RESCHEDULE_REQUEST)
    )
    assert int(reschedule_n or 0) == 2

    # 4 и 5 без активной брони: запись уже началась.
    booking.planned_date = utcnow_naive() - timedelta(hours=1)
    memory_db.commit()
    sent: list[str] = []
    for channel, reply in (
        ("vk", "4"),
        ("vk", "5"),
        ("max", "4"),
        ("max", "5"),
        ("telegram", "4"),
        ("telegram", "5"),
    ):
        _fire_webhook(memory_db, channel, reply, sent)
    contact_rows = list(
        memory_db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == EVENT_CLIENT_CONTACT_REQUEST
            )
        ).all()
    )
    assert len(contact_rows) == 3
    texts = [json.loads(row.payload_json)["text"] for row in contact_rows]
    assert all("Клиентка просит связаться" in t for t in texts)
    assert any("https://vk.com/gim555?sel=1001" in t for t in texts)
    assert any("/clients/" in t for t in texts)
    assert any("Администратор скоро напишет" in t for t in sent)
    assert any("Напишите нам" in t for t in sent)
    assert any("Как добраться" in t for t in sent)
    assert vk_community_dialog_url(1001) == "https://vk.com/gim555?sel=1001"


def test_reply_2_after_status_info_without_reminder(memory_db) -> None:
    """Ответ «2» на «Информация о брони» без напоминаний находит бронь."""
    planned = utcnow_naive() + timedelta(hours=20)
    _user, _client, booking = _seed(memory_db, planned=planned, with_send=False)
    memory_db.add(
        ClientNotificationSend(
            client_id=booking.client_id,
            booking_id=booking.id,
            channel="vk",
            event_type=EVENT_CLIENT_BOOKING_STATUS,
            target_id=1001,
            sent_at=utcnow_naive(),
        )
    )
    memory_db.commit()
    reply = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="2")
    memory_db.commit()
    memory_db.refresh(booking)
    assert reply == MSG_RESCHEDULE_REQUESTED
    assert booking.client_reschedule_requested_via == "vk"
    assert booking.client_reschedule_requested_at is not None


def test_render_reply_templates_and_after_name(memory_db) -> None:
    ensure_builtin_studio_variables(memory_db)
    save_builtin_values(memory_db, {"address": "дом 5", "studio_telegram": "https://t.me/studio"}, user_id=None)
    row = memory_db.scalar(
        select(StudioTemplateVariable).where(StudioTemplateVariable.key == "route_video")
    )
    assert row is not None
    row.public_token = "a" * 32
    row.file_path = "clip.mp4"
    memory_db.commit()
    directions = render_bot_reply(memory_db, "directions_5")
    assert directions == "Как добраться: дом 5"
    assert "/media/" not in directions
    assert "route_video" not in directions
    tg = render_bot_reply(memory_db, "contact_telegram")
    assert tg == "Напишите нам: https://t.me/studio"
    after = render_client_notify_template(
        "Спасибо, {{name}}. Дата {{date}}. Адрес {{address}}.",
        booking=SimpleNamespace(planned_date=datetime(2026, 1, 1), planned_services=[], planned_service=None, masters=[]),  # type: ignore[arg-type]
        client=SimpleNamespace(name="Катя"),  # type: ignore[arg-type]
        tz_name="UTC",
        db=memory_db,
        substitute_system=False,
        system_keys=frozenset({"name"}),
    )
    assert after == "Спасибо, Катя. Дата {{date}}. Адрес дом 5."


def test_repeat_2_and_no_booking_and_handled_cancel(memory_db) -> None:
    planned = utcnow_naive() + timedelta(hours=6)
    user, client, booking = _seed(memory_db, planned=planned)
    first = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="2")
    second = handle_client_text(memory_db, channel="max", messenger_id=2002, text="2")
    memory_db.commit()
    assert first == MSG_RESCHEDULE_REQUESTED
    assert second == MSG_RESCHEDULE_REQUESTED
    n = memory_db.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.event_type == EVENT_BOOKING_CLIENT_RESCHEDULE_REQUEST)
    )
    assert int(n or 0) == 1

    no_send_client = Client(
        name="Без записи",
        is_confirmed=True,
        vk_user_id=4040,
        max_user_id=8080,
        telegram_chat_id=9090,
    )
    memory_db.add(no_send_client)
    memory_db.commit()
    assert (
        handle_client_text(memory_db, channel="vk", messenger_id=4040, text="2")
        == "Не нашли активную запись, напишите 4, чтобы связаться с администратором"
    )
    four = handle_client_text(memory_db, channel="vk", messenger_id=4040, text="4")
    four_max = handle_client_text(memory_db, channel="max", messenger_id=8080, text="4")
    five = handle_client_text(memory_db, channel="telegram", messenger_id=9090, text="5")
    assert four == "Администратор скоро напишет вам здесь"
    assert four_max.startswith("Напишите нам:")
    assert five is not None and "Как добраться" in five

    booking.client_cancel_requested_at = utcnow_naive()
    booking.client_cancel_requested_via = "vk"
    memory_db.commit()
    mark_client_reply_handled(memory_db, booking, "cancel", user_id=user.id)
    memory_db.commit()
    memory_db.refresh(booking)
    assert booking.client_cancel_requested_at is None
    assert booking.client_cancel_handled_by_user_id == user.id
    _ = client
