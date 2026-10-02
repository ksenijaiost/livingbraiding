"""3.3: привязка Telegram — вебхук /start и одноразовые коды."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import TelegramLinkToken, User, UserRole
from app.routes.telegram_webhook import telegram_webhook
from app.settings import get_settings
from app.telegram_link import (
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    create_telegram_link_token,
    hash_telegram_link_token,
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
        username="master1",
        password_hash="x",
        display_name="Мастер",
        role=UserRole.MASTER,
        is_active=True,
        notify_enabled=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _run_webhook(db, payload: dict, *, secret_header: str | None = None):
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)

    async def _call():
        return await telegram_webhook(
            req,
            db=db,
            x_telegram_bot_api_secret_token=secret_header,
        )

    return asyncio.run(_call())


def test_webhook_valid_code_binds_chat_id(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_telegram_link_token(memory_db, u.id)
    memory_db.commit()

    sent: list[tuple[int, str]] = []

    def _fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    with patch("app.routes.telegram_webhook.send_telegram", side_effect=_fake_send):
        res = _run_webhook(
            memory_db,
            {"message": {"chat": {"id": 991122}, "text": f"/start {plain}"}},
            secret_header="sec",
        )
    assert res.status_code == 200
    memory_db.refresh(u)
    assert u.telegram_chat_id == 991122
    assert sent == [(991122, MSG_LINKED_OK)]
    tok = memory_db.scalar(select(TelegramLinkToken).where(TelegramLinkToken.user_id == u.id))
    assert tok is not None and tok.used_at is not None


def test_webhook_invalid_code_does_not_bind(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    sent: list[str] = []

    def _fake_send(chat_id: int, text: str) -> None:
        sent.append(text)

    with patch("app.routes.telegram_webhook.send_telegram", side_effect=_fake_send):
        res = _run_webhook(
            memory_db,
            {"message": {"chat": {"id": 55}, "text": "/start not-a-real-token"}},
        )
    assert res.status_code == 200
    memory_db.refresh(u)
    assert u.telegram_chat_id is None
    assert sent == [MSG_LINK_INVALID]


def test_webhook_expired_code_does_not_bind(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_telegram_link_token(memory_db, u.id)
    memory_db.commit()
    row = memory_db.scalar(
        select(TelegramLinkToken).where(TelegramLinkToken.token_hash == hash_telegram_link_token(plain))
    )
    assert row is not None
    row.expires_at = utcnow_naive() - timedelta(hours=1)
    memory_db.commit()

    with patch("app.routes.telegram_webhook.send_telegram"):
        res = _run_webhook(
            memory_db,
            {"message": {"chat": {"id": 77}, "text": f"/start {plain}"}},
        )
    assert res.status_code == 200
    memory_db.refresh(u)
    assert u.telegram_chat_id is None


def test_webhook_wrong_secret_403(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "expected")
    get_settings.cache_clear()
    res = _run_webhook(
        memory_db,
        {"message": {"chat": {"id": 1}, "text": "/start abc"}},
        secret_header="wrong",
    )
    assert res.status_code == 403


def test_webhook_code_reuse_fails(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    u = _seed_user(memory_db)
    plain, _ = create_telegram_link_token(memory_db, u.id)
    memory_db.commit()
    replies: list[str] = []

    def _fake_send(chat_id: int, text: str) -> None:
        replies.append(text)

    with patch("app.routes.telegram_webhook.send_telegram", side_effect=_fake_send):
        r1 = _run_webhook(
            memory_db,
            {"message": {"chat": {"id": 100}, "text": f"/start {plain}"}},
        )
        r2 = _run_webhook(
            memory_db,
            {"message": {"chat": {"id": 200}, "text": f"/start {plain}"}},
        )
    assert r1.status_code == 200 and r2.status_code == 200
    memory_db.refresh(u)
    assert u.telegram_chat_id == 100
    assert replies[0] == MSG_LINKED_OK
    assert replies[1] == MSG_LINK_INVALID
