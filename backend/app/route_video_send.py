"""Видео «Как нас найти» вложением в чат. В тексте ссылки на сайт нет."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.care_memo_send import _find_token, _multipart, _read_json, _telegram_call, _vk_call
from app.db.models import NotificationChannel, NotificationOutbox, NotificationOutboxStatus, StudioTemplateVariable
from app.media_store import VIDEO_MIME, media_root_dir
from app.settings import get_settings
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

ROUTE_VIDEO_KEY = "route_video"
EVENT_ROUTE_VIDEO_ERROR = "client_route_video_error"
ROUTE_VIDEO_ERROR_TEXT = "Видео „Как нас найти“ — ошибка"
_ROUTE_VIDEO_RE = re.compile(r"\{\{\s*route_video\s*\}\}")
_MAX_NOT_READY_TRIES = 8
_NOT_READY_PAUSE_SEC = 1.5


def template_wants_route_video(template: str | None) -> bool:
    return _ROUTE_VIDEO_RE.search(template or "") is not None


def strip_route_video_placeholder(text: str | None) -> str:
    """Убрать {{route_video}}. Строка, где после этого ничего не осталось, тоже уходит."""
    raw = text if isinstance(text, str) else ""
    if _ROUTE_VIDEO_RE.search(raw) is None:
        return raw
    kept: list[str] = []
    for line in raw.splitlines():
        if _ROUTE_VIDEO_RE.search(line) is None:
            kept.append(line)
            continue
        cleaned = _ROUTE_VIDEO_RE.sub(" ", line)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned:
            kept.append(cleaned)
    return "\n".join(kept)


def deliver_route_video(db: Session, row: NotificationOutbox) -> None:
    """После текста приложить видео. Ошибка видео не отменяет текст."""
    payload = _payload(row)
    if not payload.get("attach_route_video") or payload.get("route_video_done"):
        return
    payload["route_video_done"] = True
    row.payload_json = json.dumps(payload, ensure_ascii=False)
    channel = row.channel.value if hasattr(row.channel, "value") else str(row.channel or "")
    try:
        target_id = int(payload.get("target_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    try:
        if _video_row(db) is None:
            db.flush()
            return
        if target_id <= 0:
            raise RuntimeError("нет получателя")
        _send_uploaded(db, channel, target_id)
    except Exception as exc:
        _write_error(db, row, exc)
    db.flush()


def deliver_route_video_for_bot_reply(db: Session, meta: dict) -> None:
    """Ответ бота уже ушёл текстом. Видео — следом, если шаблон его просил."""
    from app.client_bot_replies import bot_reply_body

    key = str(meta.get("reply_key") or "")
    if not template_wants_route_video(bot_reply_body(db, key)):
        return
    if _video_row(db) is None:
        return
    channel = str(meta.get("channel") or "").strip().lower()
    try:
        target_id = int(meta.get("messenger_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    try:
        if target_id <= 0:
            raise RuntimeError("нет получателя")
        _send_uploaded(db, channel, target_id)
    except Exception as exc:
        _write_bot_error(db, meta, channel, exc)
    db.flush()


def _send_uploaded(db: Session, channel: str, target_id: int) -> None:
    row = _video_row(db)
    if row is None:
        return
    if channel == NotificationChannel.VK.value:
        attachment = _vk_video_attachment(db, row, target_id)
        _vk_messages_send(target_id, attachment)
        return
    if channel == NotificationChannel.MAX.value:
        token = _max_video_token(db, row)
        _max_send_video(target_id, token)
        return
    if channel == NotificationChannel.TELEGRAM.value:
        _telegram_send_video(db, target_id, row)
        return
    raise RuntimeError(f"канал {channel}")


def _vk_video_attachment(db: Session, row: StudioTemplateVariable, peer_id: int) -> str:
    cached = (row.vk_video_attachment or "").strip()
    if cached:
        return cached
    try:
        attachment = _vk_upload_video(row)
    except Exception as exc:
        logger.info("video.save недоступен, отправляю документом: %s", exc)
        attachment = _vk_upload_doc(row, peer_id)
    row.vk_video_attachment = attachment
    db.flush()
    return attachment


def _vk_upload_video(row: StudioTemplateVariable) -> str:
    saved = _vk_call(
        "video.save",
        {
            "group_id": _vk_group_id(),
            "name": "Как нас найти",
            "wallpost": "0",
        },
    )
    if not isinstance(saved, dict):
        raise RuntimeError("video.save без id")
    upload_url = str(saved.get("upload_url") or "")
    owner = saved.get("owner_id")
    video_id = saved.get("video_id")
    if not upload_url or owner is None or video_id is None:
        raise RuntimeError("video.save без upload_url")
    _post_file(upload_url, "video_file", row)
    return f"video{owner}_{video_id}"


def _vk_upload_doc(row: StudioTemplateVariable, peer_id: int) -> str:
    server = _vk_call(
        "docs.getMessagesUploadServer",
        {"type": "doc", "peer_id": str(int(peer_id))},
    )
    upload_url = str(server.get("upload_url") or "") if isinstance(server, dict) else ""
    if not upload_url:
        raise RuntimeError("docs.getMessagesUploadServer без upload_url")
    uploaded = _post_file(upload_url, "file", row)
    file_token = uploaded.get("file") if isinstance(uploaded, dict) else None
    if not isinstance(file_token, str) or not file_token.strip():
        raise RuntimeError("VK не вернул file")
    saved = _vk_call("docs.save", {"file": file_token, "title": "Как нас найти"})
    return _doc_attachment(saved)


def _doc_attachment(saved: Any) -> str:
    doc = saved
    if isinstance(saved, dict) and isinstance(saved.get("doc"), dict):
        doc = saved["doc"]
    elif isinstance(saved, list) and saved:
        first = saved[0]
        if isinstance(first, dict) and isinstance(first.get("doc"), dict):
            doc = first["doc"]
        else:
            doc = first
    if not isinstance(doc, dict) or doc.get("id") is None or doc.get("owner_id") is None:
        raise RuntimeError("docs.save без id")
    return f"doc{doc['owner_id']}_{doc['id']}"


def _vk_group_id() -> str:
    raw = (get_settings().vk_group_id or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise RuntimeError("VK_GROUP_ID не задан")
    return str(int(digits))


def _vk_messages_send(peer_id: int, attachment: str) -> None:
    _vk_call(
        "messages.send",
        {
            "peer_id": str(int(peer_id)),
            "attachment": attachment,
            "random_id": str(random.randint(1, 2_147_483_647)),
            "message": "",
        },
    )


def _max_video_token(db: Session, row: StudioTemplateVariable) -> str:
    cached = (row.max_video_token or "").strip()
    if cached:
        return cached
    settings = get_settings()
    token = (settings.max_bot_token or "").strip()
    if not token:
        raise RuntimeError("Max не настроен")
    base = (settings.max_api_base or "https://platform-api2.max.ru").rstrip("/")
    meta = _read_json(
        f"{base}/uploads?type=video",
        method="POST",
        data=b"",
        headers={"Authorization": token},
        use_max_ca=True,
    )
    meta_token = _find_token(meta)
    url = meta.get("url") if isinstance(meta, dict) else None
    if not isinstance(url, str) or not url:
        if meta_token:
            row.max_video_token = meta_token
            db.flush()
            return meta_token
        raise RuntimeError("Max не вернул адрес загрузки видео")
    uploaded_token = _post_video_file(url, row)
    got = uploaded_token or meta_token
    if not got:
        raise RuntimeError("Max не вернул token видео")
    row.max_video_token = got
    db.flush()
    return got


def _max_send_video(user_id: int, video_token: str) -> None:
    settings = get_settings()
    token = (settings.max_bot_token or "").strip()
    if not token:
        raise RuntimeError("Max не настроен")
    base = (settings.max_api_base or "https://platform-api2.max.ru").rstrip("/")
    body = json.dumps(
        {"attachments": [{"type": "video", "payload": {"token": video_token}}]},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Authorization": token, "Content-Type": "application/json; charset=utf-8"}
    url = f"{base}/messages?user_id={int(user_id)}"
    last = "attachment.not.ready"
    for attempt in range(_MAX_NOT_READY_TRIES):
        try:
            data = _read_json(url, method="POST", data=body, headers=headers, use_max_ca=True)
        except RuntimeError as exc:
            last = str(exc)
            if "attachment.not.ready" in last.lower() and attempt + 1 < _MAX_NOT_READY_TRIES:
                time.sleep(_NOT_READY_PAUSE_SEC)
                continue
            raise
        blob = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
        if "attachment.not.ready" in blob.lower():
            last = "attachment.not.ready"
            if attempt + 1 < _MAX_NOT_READY_TRIES:
                time.sleep(_NOT_READY_PAUSE_SEC)
                continue
            raise RuntimeError(last)
        return
    raise RuntimeError(last)


def _telegram_send_video(db: Session, chat_id: int, row: StudioTemplateVariable) -> None:
    cached = (row.telegram_video_file_id or "").strip()
    if cached:
        _telegram_call("sendVideo", {"chat_id": str(int(chat_id)), "video": cached})
        return
    settings = get_settings()
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    base = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/sendVideo"
    raw, content_type = _multipart(
        {"chat_id": str(int(chat_id))},
        "video",
        row.file_name or "route.mp4",
        _video_bytes(row),
        _video_mime(row),
    )
    data = _read_json(url, method="POST", data=raw, headers={"Content-Type": content_type})
    if not isinstance(data, dict) or not data.get("ok"):
        desc = data.get("description") if isinstance(data, dict) else ""
        raise RuntimeError(desc or "Telegram API error")
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    video = result.get("video") if isinstance(result.get("video"), dict) else {}
    file_id = str(video.get("file_id") or "").strip()
    if file_id:
        row.telegram_video_file_id = file_id
        db.flush()


def _post_video_file(url: str, row: StudioTemplateVariable) -> str:
    """POST файла видео. Ответ загрузчика может быть не JSON — тогда токен берут из meta."""
    import httpx

    from app.max_http import max_request

    raw, content_type = _multipart(
        {},
        "data",
        row.file_name or "route.mp4",
        _video_bytes(row),
        _video_mime(row),
    )
    try:
        status, raw_bytes = max_request(
            "POST",
            url,
            content=raw,
            headers={"Content-Type": content_type},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"сеть: {exc}") from exc
    if status >= 400:
        detail = raw_bytes.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {status}: {detail}")
    text = raw_bytes.decode("utf-8", errors="replace")
    headers = getattr(max_request, "last_headers", None) or {}
    return _token_from_upload_body(text, headers)


def _token_from_upload_body(text: str, headers: dict) -> str:
    parsed: Any = None
    if text.strip():
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    if parsed is not None:
        found = _find_token(parsed)
        if found:
            return found
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() in {"token", "x-token", "x-upload-token"} and str(value).strip():
                return str(value).strip()
    match = re.search(r"""["']?token["']?\s*[:=]\s*["']([^"'<\s]+)""", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


def _post_file(
    url: str,
    field: str,
    row: StudioTemplateVariable,
    *,
    use_max_ca: bool = False,
) -> dict[str, Any]:
    raw, content_type = _multipart(
        {},
        field,
        row.file_name or "route.mp4",
        _video_bytes(row),
        _video_mime(row),
    )
    data = _read_json(
        url,
        method="POST",
        data=raw,
        headers={"Content-Type": content_type},
        use_max_ca=use_max_ca,
    )
    return data if isinstance(data, dict) else {}


def _video_row(db: Session) -> StudioTemplateVariable | None:
    row = db.scalar(
        select(StudioTemplateVariable).where(StudioTemplateVariable.key == ROUTE_VIDEO_KEY).limit(1)
    )
    if row is None or not (row.file_path or "").strip():
        return None
    return row


def _video_mime(row: StudioTemplateVariable) -> str:
    ext = Path(row.file_path or "").suffix.lower()
    return VIDEO_MIME.get(ext) or "video/mp4"


def _video_bytes(row: StudioTemplateVariable) -> bytes:
    name = Path(row.file_path or "").name
    if not name or name != row.file_path or ".." in name:
        raise RuntimeError("Файл видео не найден")
    root = media_root_dir().resolve()
    path = (root / name).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("Файл видео не найден") from exc
    if not path.is_file():
        raise RuntimeError("Файл видео не найден")
    return path.read_bytes()


def _write_error(db: Session, parent: NotificationOutbox, exc: Exception) -> None:
    text = ROUTE_VIDEO_ERROR_TEXT
    detail = str(exc).strip()[:500] or text
    err = NotificationOutbox(
        user_id=None,
        client_id=parent.client_id,
        target_kind=parent.target_kind or "client",
        booking_id=parent.booking_id,
        visit_id=parent.visit_id,
        channel=parent.channel,
        event_type=EVENT_ROUTE_VIDEO_ERROR,
        payload_json=json.dumps(
            {"text": text, "target_kind": parent.target_kind or "client"},
            ensure_ascii=False,
        ),
        body=text,
        status=NotificationOutboxStatus.FAILED,
        error=detail,
        dedupe_key=f"rviderr:{int(parent.id)}",
        attempt_count=99,
        created_at=utcnow_naive(),
        scheduled_at=None,
        sent_at=None,
    )
    try:
        with db.begin_nested():
            db.add(err)
            db.flush()
    except IntegrityError:
        return


def _write_bot_error(db: Session, meta: dict, channel: str, exc: Exception) -> None:
    text = ROUTE_VIDEO_ERROR_TEXT
    try:
        ch = NotificationChannel(channel)
    except ValueError:
        return
    now = utcnow_naive()
    client_raw = meta.get("client_id")
    booking_raw = meta.get("booking_id")
    try:
        client_id = int(client_raw) if client_raw else None
    except (TypeError, ValueError):
        client_id = None
    try:
        booking_id = int(booking_raw) if booking_raw else None
    except (TypeError, ValueError):
        booking_id = None
    key = (
        f"rviderr:bot:{client_id}:{channel}:"
        f"{now.strftime('%Y%m%d%H%M%S%f')}:{meta.get('reply_key')}"
    )
    if len(key) > 200:
        key = key[:200]
    err = NotificationOutbox(
        user_id=None,
        client_id=client_id,
        target_kind="client",
        booking_id=booking_id,
        channel=ch,
        event_type=EVENT_ROUTE_VIDEO_ERROR,
        payload_json=json.dumps({"text": text, "target_kind": "client"}, ensure_ascii=False),
        body=text,
        status=NotificationOutboxStatus.FAILED,
        error=(str(exc).strip()[:500] or text),
        dedupe_key=key,
        attempt_count=99,
        created_at=now,
        scheduled_at=None,
        sent_at=None,
    )
    try:
        with db.begin_nested():
            db.add(err)
            db.flush()
    except IntegrityError:
        return


def _payload(row: NotificationOutbox) -> dict[str, Any]:
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
