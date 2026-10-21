"""3.39: лог вебхука Max, bot_added и разбор /admins."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.admin_chat import MSG_ADMINS_USAGE, classify_admins_text, parse_admins_code
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import AdminChatTarget, NotificationChannel
from app.routes.max_webhook import MSG_BOT_ADDED, max_webhook
from app.settings import get_settings
from app.time_utils import utcnow_naive


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _run(db, payload: dict, *, secret: str | None = "sec"):
    req = MagicMock()
    req.json = AsyncMock(return_value=payload)

    async def _call():
        return await max_webhook(req, db=db, x_max_bot_api_secret=secret)

    return asyncio.run(_call())


def test_parse_admins_mentions() -> None:
    assert parse_admins_code("/admins ABC") == "ABC"
    assert parse_admins_code("/admins@studio_bot ABC") == "ABC"
    assert parse_admins_code("@studio_bot /admins ABC") == "ABC"
    assert parse_admins_code("/admins ABC @studio_bot") == "ABC"
    assert parse_admins_code("[Бот](max://user/1) /admins ABC") == "ABC"
    code, kind = classify_admins_text("/admins")
    assert code is None and kind == "missing"
    assert classify_admins_text("просто текст") == (None, "none")


def test_bad_secret_warning(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "expected")
    get_settings.cache_clear()
    db = _db()
    with caplog.at_level(logging.WARNING, logger="app.routes.max_webhook"):
        res = _run(db, {"update_type": "message_created"}, secret="wrong")
    assert res.status_code == 403
    assert "max webhook bad secret" in caplog.text
    assert "wrong" not in caplog.text
    assert "expected" not in caplog.text
    db.close()


def test_logs_group_message_fields(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    get_settings.cache_clear()
    db = _db()
    payload = {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 55},
            "recipient": {"chat_id": 900, "chat_type": "chat", "title": "Админы"},
            "body": {"text": "@bot /admins"},
        },
    }
    with caplog.at_level(logging.INFO, logger="app.routes.max_webhook"):
        with patch("app.routes.max_webhook.send_max") as send:
            res = _run(db, payload)
    assert res.status_code == 200
    assert "update_type=message_created" in caplog.text
    assert "chat_id=900" in caplog.text
    assert "chat_type=chat" in caplog.text
    assert "sender_id=55" in caplog.text
    send.assert_called_once()
    assert MSG_ADMINS_USAGE in send.call_args.kwargs["text"] or MSG_ADMINS_USAGE in (
        send.call_args.args[-1] if send.call_args.args else ""
    )
    db.close()


def test_unknown_update_type_200(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    get_settings.cache_clear()
    db = _db()
    with caplog.at_level(logging.INFO, logger="app.routes.max_webhook"):
        res = _run(db, {"update_type": "chat_title_changed", "chat_id": 3})
    assert res.status_code == 200
    assert "update_type=chat_title_changed" in caplog.text
    assert "ignored update_type=chat_title_changed" in caplog.text
    db.close()


def test_bot_added_greets(monkeypatch) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    get_settings.cache_clear()
    db = _db()
    payload = {
        "update_type": "bot_added",
        "chat_id": 77,
        "user": {"user_id": 1, "first_name": "Анна"},
        "is_channel": False,
    }
    with patch("app.routes.max_webhook.send_max") as send:
        res = _run(db, payload)
    assert res.status_code == 200
    send.assert_called_once()
    assert send.call_args.kwargs["chat_id"] == 77
    assert "Привет!" in send.call_args.kwargs["text"]
    assert MSG_BOT_ADDED.startswith("Привет!")
    db.close()


def test_bot_removed_logs_linked_without_delete(monkeypatch, caplog) -> None:
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec")
    get_settings.cache_clear()
    db = _db()
    db.add(
        AdminChatTarget(
            channel=NotificationChannel.MAX.value,
            chat_id=77,
            title="Админы",
            linked_at=utcnow_naive(),
        )
    )
    db.commit()
    with caplog.at_level(logging.INFO, logger="app.routes.max_webhook"):
        res = _run(db, {"update_type": "bot_removed", "chat_id": 77})
    assert res.status_code == 200
    assert "bot_removed linked admin chat" in caplog.text
    still = db.scalar(select(AdminChatTarget).where(AdminChatTarget.channel == "max"))
    assert still is not None
    db.close()


def test_show_prints_update_types(monkeypatch, capsys) -> None:
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    monkeypatch.setenv("MAX_API_BASE", "https://platform-api2.max.ru")
    get_settings.cache_clear()
    body = json.dumps(
        {
            "subscriptions": [
                {
                    "url": "https://crm.example.com/webhooks/max",
                    "update_types": ["message_created", "bot_started"],
                }
            ]
        }
    ).encode()

    def fake_request(method, url, *, content=None, headers=None, timeout=8):
        assert method == "GET"
        return 200, body

    monkeypatch.setattr("app.scripts.max_subscribe.max_request", fake_request)
    from app.scripts.max_subscribe import main

    assert main(["--show"]) == 0
    out = capsys.readouterr().out
    assert "update_types=message_created,bot_started" in out
