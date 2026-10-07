"""Уведомления: вебхук Max (привязка аккаунта, без логина CRM)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.notifications import send_max
from app.settings import get_settings
from app.telegram_link import (
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    MSG_MAX_TAKEN,
    MaxUserTakenError,
    bind_max_user,
    consume_max_link_token,
    parse_max_link_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_webhook_secret(header_value: str | None) -> JSONResponse | None:
    expected = (get_settings().max_webhook_secret or "").strip()
    if not expected:
        # Без секрета в env не принимаем апдейты (безопаснее, чем открытый webhook).
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    got = (header_value or "").strip()
    if got != expected:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return None


def _extract_update(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    # Иногда апдейт лежит в корне, иногда в "update".
    if payload.get("update_type"):
        return payload
    inner = payload.get("update")
    if isinstance(inner, dict) and inner.get("update_type"):
        return inner
    return None


def _max_user_id_from_update(upd: dict[str, Any]) -> int | None:
    user = upd.get("user") if isinstance(upd.get("user"), dict) else None
    if user and user.get("user_id") is not None:
        try:
            return int(user["user_id"])
        except (TypeError, ValueError):
            pass
    # fallback: message.from / message.sender
    message = upd.get("message") if isinstance(upd.get("message"), dict) else None
    if message:
        for key in ("sender", "from", "user"):
            obj = message.get(key)
            if isinstance(obj, dict) and obj.get("user_id") is not None:
                try:
                    return int(obj["user_id"])
                except (TypeError, ValueError):
                    pass
            if isinstance(obj, dict) and obj.get("id") is not None:
                try:
                    return int(obj["id"])
                except (TypeError, ValueError):
                    pass
    return None


def _message_text(upd: dict[str, Any]) -> str | None:
    message = upd.get("message") if isinstance(upd.get("message"), dict) else None
    if not message:
        return None
    body = message.get("body") if isinstance(message.get("body"), dict) else None
    if body and isinstance(body.get("text"), str):
        return body["text"]
    text = message.get("text")
    return text if isinstance(text, str) else None


@router.post("/webhooks/max")
async def max_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_max_bot_api_secret: str | None = Header(default=None, alias="X-Max-Bot-Api-Secret"),
) -> Response:
    denied = _check_webhook_secret(x_max_bot_api_secret)
    if denied is not None:
        return denied

    try:
        payload: Any = await request.json()
    except Exception:
        logger.exception("max webhook: bad json")
        return JSONResponse({"ok": True})

    try:
        upd = _extract_update(payload)
        if upd is None:
            return JSONResponse({"ok": True})

        update_type = str(upd.get("update_type") or "").strip()
        code: str | None = None
        if update_type == "bot_started":
            code = parse_max_link_code(payload=str(upd.get("payload") or "") or None)
        elif update_type == "message_created":
            code = parse_max_link_code(text=_message_text(upd))
        else:
            return JSONResponse({"ok": True})

        max_uid = _max_user_id_from_update(upd)
        if code is None or max_uid is None:
            return JSONResponse({"ok": True})

        user = consume_max_link_token(db, code)
        if user is None:
            db.commit()
            try:
                send_max(max_uid, MSG_LINK_INVALID)
            except Exception:
                logger.exception("max webhook: reply invalid failed user_id=%s", max_uid)
            return JSONResponse({"ok": True})

        try:
            bind_max_user(db, user, max_uid)
            db.commit()
        except MaxUserTakenError:
            db.rollback()
            try:
                send_max(max_uid, MSG_MAX_TAKEN)
            except Exception:
                logger.exception("max webhook: reply taken failed user_id=%s", max_uid)
            return JSONResponse({"ok": True})

        try:
            send_max(max_uid, MSG_LINKED_OK)
        except Exception:
            logger.exception("max webhook: reply ok failed user_id=%s", max_uid)
    except Exception:
        logger.exception("max webhook: handler error")
        try:
            db.rollback()
        except Exception:
            pass

    return JSONResponse({"ok": True})
