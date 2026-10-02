"""Уведомления: вебхук Telegram (привязка аккаунта, без логина CRM)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.notifications import send_telegram
from app.settings import get_settings
from app.telegram_link import (
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    bind_telegram_chat,
    consume_telegram_link_token,
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
        text = message.get("text")
        code = parse_telegram_start_code(text if isinstance(text, str) else None)
        if code is None or chat_id is None:
            return JSONResponse({"ok": True})

        user = consume_telegram_link_token(db, code)
        if user is None:
            db.commit()
            try:
                send_telegram(int(chat_id), MSG_LINK_INVALID)
            except Exception:
                logger.exception("telegram webhook: reply invalid failed chat_id=%s", chat_id)
            return JSONResponse({"ok": True})

        bind_telegram_chat(db, user, int(chat_id))
        db.commit()
        try:
            send_telegram(int(chat_id), MSG_LINKED_OK)
        except Exception:
            logger.exception("telegram webhook: reply ok failed chat_id=%s", chat_id)
    except Exception:
        logger.exception("telegram webhook: handler error")
        try:
            db.rollback()
        except Exception:
            pass

    return JSONResponse({"ok": True})
