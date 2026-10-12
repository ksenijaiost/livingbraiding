"""Вебхук Max: привязка сотрудника/клиента, /admins, ответы 1/3."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.admin_chat import try_bind_admin_chat_from_text
from app.db.session import get_db
from app.messenger_inbound import process_inbound_message
from app.notifications import send_max
from app.settings import get_settings
from app.telegram_link import (
    CHANNEL_MAX,
    MSG_CLIENT_LINKED_OK,
    MSG_CLIENT_MAX_TAKEN,
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    MSG_MAX_TAKEN,
    ClientChannelTakenError,
    MaxUserTakenError,
    bind_client_max,
    bind_max_user,
    consume_any_link_token,
    parse_max_link_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_webhook_secret(header_value: str | None) -> JSONResponse | None:
    expected = (get_settings().max_webhook_secret or "").strip()
    if not expected:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    got = (header_value or "").strip()
    if got != expected:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return None


def _extract_update(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
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


def _chat_id_from_update(upd: dict[str, Any]) -> int | None:
    if upd.get("chat_id") is not None:
        try:
            return int(upd["chat_id"])
        except (TypeError, ValueError):
            pass
    message = upd.get("message") if isinstance(upd.get("message"), dict) else None
    if message and message.get("chat_id") is not None:
        try:
            return int(message["chat_id"])
        except (TypeError, ValueError):
            pass
    chat = upd.get("chat") if isinstance(upd.get("chat"), dict) else None
    if chat and chat.get("chat_id") is not None:
        try:
            return int(chat["chat_id"])
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


def _try_bind(db: Session, *, max_uid: int, code: str) -> None:
    user, client = consume_any_link_token(db, code, channel=CHANNEL_MAX)
    if user is None and client is None:
        db.commit()
        try:
            send_max(max_uid, MSG_LINK_INVALID)
        except Exception:
            logger.exception("max webhook: reply invalid failed")
        return
    if user is not None:
        try:
            bind_max_user(db, user, max_uid)
            db.commit()
            send_max(max_uid, MSG_LINKED_OK)
        except MaxUserTakenError:
            db.rollback()
            try:
                send_max(max_uid, MSG_MAX_TAKEN)
            except Exception:
                logger.exception("max webhook: reply taken failed")
        except Exception:
            logger.exception("max webhook: bind user failed")
            try:
                db.rollback()
            except Exception:
                pass
        return
    try:
        bind_client_max(db, client, max_uid)
        db.commit()
        try:
            send_max(max_uid, MSG_CLIENT_LINKED_OK)
        except Exception:
            logger.exception("max webhook: client reply ok failed")
    except ClientChannelTakenError:
        db.rollback()
        try:
            send_max(max_uid, MSG_CLIENT_MAX_TAKEN)
        except Exception:
            logger.exception("max webhook: client taken reply failed")


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
        text = _message_text(upd) if update_type == "message_created" else None
        chat_id = _chat_id_from_update(upd)
        max_uid = _max_user_id_from_update(upd)

        # Групповой чат: /admins (chat_id есть и отличается от user dialog — если оба, пробуем admins)
        if update_type == "message_created" and chat_id is not None and text:
            reply = try_bind_admin_chat_from_text(
                db, channel=CHANNEL_MAX, chat_id=int(chat_id), text=text, title=None
            )
            if reply:
                try:
                    send_max(text=reply, chat_id=int(chat_id))
                except Exception:
                    logger.exception("max webhook: admin chat reply failed")
                return JSONResponse({"ok": True})

        code: str | None = None
        if update_type == "bot_started":
            code = parse_max_link_code(payload=str(upd.get("payload") or "") or None)
        elif update_type == "message_created":
            code = parse_max_link_code(text=text)
        else:
            return JSONResponse({"ok": True})

        if code is not None and max_uid is not None:
            _try_bind(db, max_uid=max_uid, code=code)
            return JSONResponse({"ok": True})

        if update_type == "message_created" and max_uid is not None and text:
            process_inbound_message(db, channel=CHANNEL_MAX, messenger_id=int(max_uid), text=text)
    except Exception:
        logger.exception("max webhook: handler error")
        try:
            db.rollback()
        except Exception:
            pass

    return JSONResponse({"ok": True})
