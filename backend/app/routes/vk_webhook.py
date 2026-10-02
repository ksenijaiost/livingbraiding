"""Callback API VK: confirmation и привязка мастера (без логина CRM)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.notifications import send_vk
from app.settings import get_settings
from app.telegram_link import (
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    bind_vk_user,
    consume_vk_link_token,
    parse_vk_link_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_OK = PlainTextResponse("ok", status_code=200)


def _secret_ok(payload: dict[str, Any]) -> bool:
    expected = (get_settings().vk_secret_key or "").strip()
    if not expected:
        # Без секрета в env — принимаем (локально); на проде секрет обязателен в настройках Callback.
        return True
    got = str(payload.get("secret") or "").strip()
    return got == expected


def _extract_message_fields(obj: Any) -> tuple[int | None, str | None, str | None, str | None]:
    """from_id, text, ref, payload из object message_new / вложенного message."""
    if not isinstance(obj, dict):
        return None, None, None, None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if not isinstance(msg, dict):
        return None, None, None, None
    from_id = msg.get("from_id")
    if from_id is None:
        from_id = msg.get("user_id")
    try:
        fid = int(from_id) if from_id is not None else None
    except (TypeError, ValueError):
        fid = None
    text = msg.get("text") if isinstance(msg.get("text"), str) else None
    ref = msg.get("ref") if isinstance(msg.get("ref"), str) else None
    payload = msg.get("payload") if isinstance(msg.get("payload"), str) else None
    return fid, text, ref, payload


def _try_bind_and_reply(db: Session, *, from_id: int, code: str) -> None:
    user = consume_vk_link_token(db, code)
    if user is None:
        db.commit()
        try:
            send_vk(int(from_id), MSG_LINK_INVALID)
        except Exception:
            logger.exception("vk webhook: reply invalid failed from_id=%s", from_id)
        return
    bind_vk_user(db, user, int(from_id))
    db.commit()
    try:
        send_vk(int(from_id), MSG_LINKED_OK)
    except Exception:
        logger.exception("vk webhook: reply ok failed from_id=%s", from_id)


@router.post("/webhooks/vk")
async def vk_webhook(request: Request, db: Session = Depends(get_db)) -> Response:
    try:
        payload: Any = await request.json()
    except Exception:
        logger.exception("vk webhook: bad json")
        return _OK

    if not isinstance(payload, dict):
        return _OK

    event_type = str(payload.get("type") or "").strip()

    if event_type == "confirmation":
        # Confirmation при настройке Callback — без проверки secret (VK может не слать).
        code = (get_settings().vk_confirmation_code or "").strip()
        return PlainTextResponse(code or "", status_code=200)

    if not _secret_ok(payload):
        return PlainTextResponse("forbidden", status_code=403)

    try:
        if event_type == "message_new":
            from_id, text, ref, pl = _extract_message_fields(payload.get("object"))
            code = parse_vk_link_code(text, ref=ref, payload=pl)
            if from_id is not None and from_id > 0 and code:
                _try_bind_and_reply(db, from_id=from_id, code=code)
        elif event_type == "message_allow":
            obj = payload.get("object") if isinstance(payload.get("object"), dict) else {}
            try:
                from_id = int(obj.get("user_id")) if obj.get("user_id") is not None else None
            except (TypeError, ValueError):
                from_id = None
            ref = obj.get("key") if isinstance(obj.get("key"), str) else None
            if ref is None and isinstance(obj.get("ref"), str):
                ref = obj.get("ref")
            code = parse_vk_link_code(None, ref=ref, payload=None)
            if from_id is not None and from_id > 0 and code:
                _try_bind_and_reply(db, from_id=from_id, code=code)
    except Exception:
        logger.exception("vk webhook: handler error type=%s", event_type)
        try:
            db.rollback()
        except Exception:
            pass

    return _OK
