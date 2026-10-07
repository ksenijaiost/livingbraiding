"""Callback API VK: confirmation, привязка, /admins, ответы 1/3."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.admin_chat import try_bind_admin_chat_from_text
from app.db.session import get_db
from app.messenger_inbound import process_inbound_message
from app.notifications import send_vk
from app.settings import get_settings
from app.telegram_link import (
    CHANNEL_VK,
    MSG_CLIENT_LINKED_OK,
    MSG_LINK_INVALID,
    MSG_LINKED_OK,
    ClientChannelTakenError,
    bind_client_vk,
    bind_vk_user,
    consume_any_link_token,
    parse_vk_link_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_OK = PlainTextResponse("ok", status_code=200)
VK_CHAT_PEER_OFFSET = 2_000_000_000


def _secret_ok(payload: dict[str, Any]) -> bool:
    expected = (get_settings().vk_secret_key or "").strip()
    if not expected:
        return True
    got = str(payload.get("secret") or "").strip()
    return got == expected


def _extract_message_fields(obj: Any) -> tuple[int | None, str | None, str | None, str | None, int | None]:
    """from_id, text, ref, payload, peer_id."""
    if not isinstance(obj, dict):
        return None, None, None, None, None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    if not isinstance(msg, dict):
        return None, None, None, None, None
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
    peer_raw = msg.get("peer_id")
    try:
        peer_id = int(peer_raw) if peer_raw is not None else None
    except (TypeError, ValueError):
        peer_id = None
    return fid, text, ref, payload, peer_id


def _try_bind_and_reply(db: Session, *, from_id: int, code: str) -> None:
    user, client = consume_any_link_token(db, code, channel=CHANNEL_VK)
    if user is None and client is None:
        db.commit()
        try:
            send_vk(int(from_id), MSG_LINK_INVALID)
        except Exception:
            logger.exception("vk webhook: reply invalid failed from_id=%s", from_id)
        return
    if user is not None:
        bind_vk_user(db, user, int(from_id))
        db.commit()
        try:
            send_vk(int(from_id), MSG_LINKED_OK)
        except Exception:
            logger.exception("vk webhook: reply ok failed from_id=%s", from_id)
        return
    try:
        bind_client_vk(db, client, int(from_id))
        db.commit()
        try:
            send_vk(int(from_id), MSG_CLIENT_LINKED_OK)
        except Exception:
            logger.exception("vk webhook: client reply ok failed from_id=%s", from_id)
    except ClientChannelTakenError as e:
        db.rollback()
        try:
            send_vk(int(from_id), str(e))
        except Exception:
            logger.exception("vk webhook: client taken reply failed")


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
        code = (get_settings().vk_confirmation_code or "").strip()
        return PlainTextResponse(code or "", status_code=200)

    if not _secret_ok(payload):
        return PlainTextResponse("forbidden", status_code=403)

    try:
        if event_type == "message_new":
            from_id, text, ref, pl, peer_id = _extract_message_fields(payload.get("object"))
            # Беседа админов
            if peer_id is not None and peer_id >= VK_CHAT_PEER_OFFSET:
                reply = try_bind_admin_chat_from_text(
                    db, channel=CHANNEL_VK, chat_id=int(peer_id), text=text, title=None
                )
                if reply:
                    try:
                        send_vk(text=reply, peer_id=int(peer_id))
                    except Exception:
                        logger.exception("vk webhook: admin chat reply failed")
                return _OK

            code = parse_vk_link_code(text, ref=ref, payload=pl)
            if from_id is not None and from_id > 0 and code:
                _try_bind_and_reply(db, from_id=from_id, code=code)
            elif from_id is not None and from_id > 0 and text:
                process_inbound_message(db, channel=CHANNEL_VK, messenger_id=int(from_id), text=text)
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
