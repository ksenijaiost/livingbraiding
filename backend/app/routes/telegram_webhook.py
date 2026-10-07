"""Вебхук Telegram: привязка сотрудника/клиента, /admins, ответы 1/3."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.admin_chat import try_bind_admin_chat_from_text
from app.db.session import get_db
from app.messenger_inbound import process_inbound_message
from app.notifications import send_telegram
from app.settings import get_settings
from app.telegram_link import (
    CHANNEL_TELEGRAM,
    MSG_CLIENT_LINKED_OK,
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    ClientChannelTakenError,
    bind_client_telegram,
    bind_telegram_chat,
    consume_any_link_token,
    parse_telegram_start_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_webhook_secret(header_value: str | None) -> JSONResponse | None:
    expected = (get_settings().telegram_webhook_secret or "").strip()
    if not expected:
        return None
    got = (header_value or "").strip()
    if got != expected:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return None


@router.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    denied = _check_webhook_secret(x_telegram_bot_api_secret_token)
    if denied is not None:
        return denied

    try:
        payload: Any = await request.json()
    except Exception:
        logger.exception("telegram webhook: bad json")
        return JSONResponse({"ok": True})

    try:
        message = (payload or {}).get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return JSONResponse({"ok": True})
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = chat.get("id")
        text = message.get("text") if isinstance(message.get("text"), str) else None
        if chat_id is None:
            return JSONResponse({"ok": True})
        chat_type = str(chat.get("type") or "").strip().lower()
        title = chat.get("title") if isinstance(chat.get("title"), str) else None

        # Группа: привязка чата админов
        if chat_type in ("group", "supergroup"):
            reply = try_bind_admin_chat_from_text(
                db,
                channel=CHANNEL_TELEGRAM,
                chat_id=int(chat_id),
                text=text,
                title=title,
            )
            if reply:
                try:
                    send_telegram(int(chat_id), reply)
                except Exception:
                    logger.exception("telegram webhook: admin chat reply failed")
            return JSONResponse({"ok": True})

        # Личка: код привязки /start
        code = parse_telegram_start_code(text)
        if code is not None:
            user, client = consume_any_link_token(db, code, channel=CHANNEL_TELEGRAM)
            if user is None and client is None:
                db.commit()
                try:
                    send_telegram(int(chat_id), MSG_LINK_INVALID)
                except Exception:
                    logger.exception("telegram webhook: reply invalid failed")
                return JSONResponse({"ok": True})
            if user is not None:
                bind_telegram_chat(db, user, int(chat_id))
                db.commit()
                try:
                    send_telegram(int(chat_id), MSG_LINKED_OK)
                except Exception:
                    logger.exception("telegram webhook: reply ok failed")
                return JSONResponse({"ok": True})
            try:
                bind_client_telegram(db, client, int(chat_id))
                db.commit()
                try:
                    send_telegram(int(chat_id), MSG_CLIENT_LINKED_OK)
                except Exception:
                    logger.exception("telegram webhook: client reply ok failed")
            except ClientChannelTakenError as e:
                db.rollback()
                try:
                    send_telegram(int(chat_id), str(e))
                except Exception:
                    logger.exception("telegram webhook: client taken reply failed")
            return JSONResponse({"ok": True})

        # Ответ клиента 1/3
        from_user = message.get("from") if isinstance(message.get("from"), dict) else {}
        from_id = from_user.get("id")
        messenger_id = int(from_id) if from_id is not None else int(chat_id)
        process_inbound_message(
            db, channel=CHANNEL_TELEGRAM, messenger_id=messenger_id, text=text
        )
    except Exception:
        logger.exception("telegram webhook: handler error")
        try:
            db.rollback()
        except Exception:
            pass

    return JSONResponse({"ok": True})
