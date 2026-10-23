"""Вебхук Max: привязка сотрудника/клиента, /admins, ответы 1/3."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.admin_chat import (
    CHAT_KIND_ADMIN,
    CHAT_KIND_GENERAL,
    classify_chat_link_text,
    get_admin_chat_target,
    try_bind_admin_chat_from_text,
)
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

MSG_BOT_ADDED = (
    "Привет! Чтобы подключить этот чат для уведомлений админов, возьмите код в CRM "
    "(Настройки → чат админов) и отправьте сюда сообщением: /admins КОД. "
    "Если я не отвечаю, проверьте, что я администратор чата."
)

_GROUP_CHAT_TYPES = frozenset({"chat", "channel"})


def _check_webhook_secret(header_value: str | None) -> JSONResponse | None:
    expected = (get_settings().max_webhook_secret or "").strip()
    if not expected:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    got = (header_value or "").strip()
    if got != expected:
        logger.warning("max webhook bad secret")
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


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message(upd: dict[str, Any]) -> dict[str, Any] | None:
    message = upd.get("message")
    return message if isinstance(message, dict) else None


def _recipient(upd: dict[str, Any]) -> dict[str, Any]:
    message = _message(upd)
    if message and isinstance(message.get("recipient"), dict):
        return message["recipient"]
    chat = upd.get("chat")
    if isinstance(chat, dict):
        return chat
    return {}


def _chat_id_from_update(upd: dict[str, Any]) -> int | None:
    for value in (
        upd.get("chat_id"),
        _recipient(upd).get("chat_id"),
        (_message(upd) or {}).get("chat_id"),
    ):
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _chat_type_from_update(upd: dict[str, Any]) -> str:
    recipient = _recipient(upd)
    for key in ("chat_type", "type"):
        raw = recipient.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    raw = upd.get("chat_type")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    if upd.get("is_channel") is True:
        return "channel"
    return ""


def _chat_title_from_update(upd: dict[str, Any]) -> str | None:
    candidates: list[object] = [_recipient(upd), upd.get("chat"), upd]
    for src in candidates:
        if not isinstance(src, dict):
            continue
        for key in ("title", "chat_title", "name"):
            raw = src.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()[:200]
    return None


def _is_group_chat(chat_type: str) -> bool:
    return chat_type.strip().lower() in _GROUP_CHAT_TYPES


def _text_preview(text: str | None) -> str:
    raw = (text or "").replace("\r", " ").replace("\n", " ").strip()
    return raw[:60] or "-"


def _log_update(
    *,
    update_type: str,
    chat_id: int | None,
    chat_type: str,
    sender_id: int | None,
    text: str | None,
) -> None:
    logger.info(
        "max webhook update_type=%s chat_id=%s chat_type=%s sender_id=%s text=%s",
        update_type or "-",
        chat_id if chat_id is not None else "-",
        chat_type or "-",
        sender_id if sender_id is not None else "-",
        _text_preview(text),
    )


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
            _log_update(update_type="", chat_id=None, chat_type="", sender_id=None, text=None)
            return JSONResponse({"ok": True})

        update_type = str(upd.get("update_type") or "").strip()
        text = _message_text(upd) if update_type == "message_created" else None
        chat_id = _chat_id_from_update(upd)
        chat_type = _chat_type_from_update(upd)
        max_uid = _max_user_id_from_update(upd)
        title = _chat_title_from_update(upd)
        _log_update(
            update_type=update_type,
            chat_id=chat_id,
            chat_type=chat_type,
            sender_id=max_uid,
            text=text,
        )

        if update_type == "bot_added" and chat_id is not None:
            logger.info(
                "max webhook bot_added chat_id=%s title=%s",
                int(chat_id),
                title or "-",
            )
            try:
                send_max(text=MSG_BOT_ADDED, chat_id=int(chat_id))
            except Exception:
                logger.exception("max webhook: bot_added reply failed chat_id=%s", chat_id)
            return JSONResponse({"ok": True})

        if update_type == "bot_removed" and chat_id is not None:
            linked = get_admin_chat_target(
                db, CHANNEL_MAX, chat_kind=CHAT_KIND_ADMIN
            ) or get_admin_chat_target(db, CHANNEL_MAX, chat_kind=CHAT_KIND_GENERAL)
            if linked is not None and int(linked.chat_id) == int(chat_id):
                logger.info(
                    "max webhook bot_removed linked admin chat chat_id=%s title=%s",
                    int(chat_id),
                    (linked.title or title or "-"),
                )
            else:
                logger.info("max webhook bot_removed chat_id=%s title=%s", int(chat_id), title or "-")
            return JSONResponse({"ok": True})

        # Группа/канал: /admins и /general. В диалоге (chat_type=dialog) команду не привязываем.
        # Если chat_type пустой, но текст — команда и есть chat_id, тоже считаем группой.
        admins_kind = classify_chat_link_text(text)[2] if text else "none"
        group_admins = _is_group_chat(chat_type) or (
            not chat_type and chat_id is not None and admins_kind != "none"
        )
        if update_type == "message_created" and chat_id is not None and text and group_admins:
            reply = try_bind_admin_chat_from_text(
                db,
                channel=CHANNEL_MAX,
                chat_id=int(chat_id),
                text=text,
                title=title,
            )
            if reply:
                try:
                    send_max(text=reply, chat_id=int(chat_id))
                except Exception:
                    logger.exception("max webhook: admin chat reply failed")
                return JSONResponse({"ok": True})
            return JSONResponse({"ok": True})

        code: str | None = None
        if update_type == "bot_started":
            code = parse_max_link_code(payload=str(upd.get("payload") or "") or None)
        elif update_type == "message_created":
            code = parse_max_link_code(text=text)
        else:
            logger.info("max webhook ignored update_type=%s", update_type or "-")
            return JSONResponse({"ok": True})

        if code is not None and max_uid is not None:
            _try_bind(db, max_uid=max_uid, code=code)
            return JSONResponse({"ok": True})

        if update_type == "bot_started" and max_uid is not None:
            from app.messenger_inbound import find_client_by_channel_id, find_user_by_channel_id

            if (
                find_client_by_channel_id(db, CHANNEL_MAX, int(max_uid)) is None
                and find_user_by_channel_id(db, CHANNEL_MAX, int(max_uid)) is None
            ):
                process_inbound_message(
                    db, channel=CHANNEL_MAX, messenger_id=int(max_uid), text="/start"
                )
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
