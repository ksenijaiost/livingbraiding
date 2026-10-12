"""3.8: VK Callback API — confirmation, secret, привязка vk_user_id."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.booking_notifications import notify_booking_created
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
    TelegramLinkToken,
    User,
    UserRole,
    VisitMastersScope,
)
from app.notifications import explain_outbox_error, format_vk_api_error
from app.routes.vk_webhook import vk_webhook
from app.settings import get_settings
from app.telegram_link import (
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    create_vk_link_token,
    hash_link_token,
)
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


def _seed_user(db) -> User:
    u = User(
        username="master_vk",
        password_hash="x",
        display_name="Мастер VK",
        role=UserRole.MASTER,
        is_active=True,
        notify_enabled=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _run_vk(db, payload: dict):
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)

    async def _call():
        return await vk_webhook(req, db=db)

    return asyncio.run(_call())


def test_confirmation_returns_code(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_CONFIRMATION_CODE", "conf123")
    get_settings.cache_clear()
    res = _run_vk(memory_db, {"type": "confirmation", "group_id": 1})
    assert res.status_code == 200
    assert res.body == b"conf123"


def test_wrong_secret_403(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "expected")
    get_settings.cache_clear()
    res = _run_vk(
        memory_db,
        {"type": "message_new", "secret": "wrong", "object": {"message": {"from_id": 1, "text": "x"}}},
    )
    assert res.status_code == 403


def test_message_new_valid_code_binds(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "sec")
    monkeypatch.setenv("VK_GROUP_TOKEN", "tok")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_vk_link_token(memory_db, u.id)
    memory_db.commit()
    sent: list[tuple[int, str]] = []

    def _fake(uid: int, text: str) -> None:
        sent.append((uid, text))

    with patch("app.routes.vk_webhook.send_vk", side_effect=_fake):
        res = _run_vk(
            memory_db,
            {
                "type": "message_new",
                "secret": "sec",
                "object": {"message": {"from_id": 777001, "text": f"привязка {plain}"}},
            },
        )
    assert res.status_code == 200
    assert res.body == b"ok"
    memory_db.refresh(u)
    assert u.vk_user_id == 777001
    assert sent == [(777001, MSG_LINKED_OK)]
    tok = memory_db.scalar(select(TelegramLinkToken).where(TelegramLinkToken.user_id == u.id))
    assert tok is not None and tok.used_at is not None and tok.channel == "vk"


def test_message_new_ref_binds(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_vk_link_token(memory_db, u.id)
    memory_db.commit()
    with patch("app.routes.vk_webhook.send_vk"):
        res = _run_vk(
            memory_db,
            {
                "type": "message_new",
                "object": {"message": {"from_id": 42, "text": "hi", "ref": plain}},
            },
        )
    assert res.status_code == 200
    memory_db.refresh(u)
    assert u.vk_user_id == 42


def test_invalid_expired_reuse(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_SECRET_KEY", "")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    replies: list[str] = []

    def _fake(uid: int, text: str) -> None:
        replies.append(text)

    with patch("app.routes.vk_webhook.send_vk", side_effect=_fake):
        r_bad = _run_vk(
            memory_db,
            {"type": "message_new", "object": {"message": {"from_id": 1, "text": "привязка nope"}}},
        )
    assert r_bad.status_code == 200
    assert replies[-1] == MSG_LINK_INVALID
    memory_db.refresh(u)
    assert u.vk_user_id is None

    plain, _ = create_vk_link_token(memory_db, u.id)
    memory_db.commit()
    row = memory_db.scalar(
        select(TelegramLinkToken).where(TelegramLinkToken.token_hash == hash_link_token(plain))
    )
    assert row is not None
    row.expires_at = utcnow_naive() - timedelta(hours=1)
    memory_db.commit()
    with patch("app.routes.vk_webhook.send_vk", side_effect=_fake):
        _run_vk(
            memory_db,
            {"type": "message_new", "object": {"message": {"from_id": 2, "text": plain}}},
        )
    memory_db.refresh(u)
    assert u.vk_user_id is None

    plain2, _ = create_vk_link_token(memory_db, u.id)
    memory_db.commit()
    with patch("app.routes.vk_webhook.send_vk", side_effect=_fake):
        _run_vk(
            memory_db,
            {"type": "message_new", "object": {"message": {"from_id": 3, "text": f"привязка {plain2}"}}},
        )
        _run_vk(
            memory_db,
            {"type": "message_new", "object": {"message": {"from_id": 4, "text": f"привязка {plain2}"}}},
        )
    memory_db.refresh(u)
    assert u.vk_user_id == 3
    assert replies[-1] == MSG_LINK_INVALID


def test_booking_vk_send_and_tg_independent(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("VK_GROUP_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg")
    get_settings.cache_clear()
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username="both",
        password_hash="x",
        display_name="Both",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=111,
        vk_user_id=222,
        notify_enabled=True,
    )
    c = Client(name="C", phone="+79991234567", is_confirmed=True)
    memory_db.add_all([u, c])
    memory_db.flush()
    apply_notify_prefs_from_legacy_flag(u, [UserRole.MASTER], enabled=True)
    memory_db.commit()
    memory_db.refresh(u)
    memory_db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=utcnow_naive() + timedelta(hours=48),
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    memory_db.add(b)
    memory_db.commit()
    memory_db.refresh(b)
    memory_db.add(BookingMaster(booking_id=b.id, master_id=u.id))
    memory_db.commit()

    tg_sent: list[int] = []
    vk_calls = {"n": 0}

    def _tg(chat_id: int, text: str) -> None:
        tg_sent.append(chat_id)

    def _vk_fail(uid: int, text: str) -> None:
        vk_calls["n"] += 1
        raise RuntimeError(format_vk_api_error({"error_code": 901, "error_msg": "Can't send"}))

    with patch("app.notifications.send_telegram", side_effect=_tg):
        with patch("app.notifications.send_vk", side_effect=_vk_fail):
            notify_booking_created(memory_db, int(b.id))

    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    assert len(rows) == 2
    by_ch = {r.channel: r for r in rows}
    assert by_ch[NotificationChannel.TELEGRAM].status == NotificationOutboxStatus.SENT
    assert by_ch[NotificationChannel.VK].status == NotificationOutboxStatus.FAILED
    assert tg_sent == [111]
    assert vk_calls["n"] == 1
    memory_db.refresh(b)
    assert b.status == BookingStatus.PENDING_CONFIRMATION


def test_explain_vk_error() -> None:
    msg = explain_outbox_error("VK API 901: Can't send", channel="vk")
    assert "разрешен" in msg.lower() or "901" in msg
