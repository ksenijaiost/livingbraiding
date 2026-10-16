"""Вложение памяток в уже отправленный текст: VK, Max, Telegram."""

from __future__ import annotations

import json
import random
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import CareMemo, NotificationChannel, NotificationOutbox, NotificationOutboxStatus
from app.media_store import media_root_dir
from app.settings import get_settings
from app.time_utils import utcnow_naive

EVENT_CARE_MEMO_ERROR = "client_care_memo_error"
_HTTP_TIMEOUT = 20


def deliver_care_memos(db: Session, row: NotificationOutbox) -> None:
    """После текста приложить картинки. Ошибка картинки не отменяет текст."""
    payload = _payload(row)
    if payload.get("care_memos_done"):
        return
    raw_ids = payload.get("care_memo_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        return
    memos: list[CareMemo] = []
    for raw in raw_ids:
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            continue
        memo = db.get(CareMemo, mid)
        if memo is not None:
            memos.append(memo)
    target_raw = payload.get("target_id")
    try:
        target_id = int(target_raw)
    except (TypeError, ValueError):
        target_id = 0
    channel = row.channel.value if hasattr(row.channel, "value") else str(row.channel or "")
    if target_id <= 0:
        for memo in memos:
            _write_error(db, row, memo, RuntimeError("нет получателя"))
        names: list[str] = []
    else:
        names, errors = _send_images(db, channel, target_id, memos)
        for memo, exc in errors:
            _write_error(db, row, memo, exc)
    payload["care_memos_attached"] = names
    payload["care_memos_done"] = True
    row.payload_json = json.dumps(payload, ensure_ascii=False)
    db.flush()


def _send_images(
    db: Session,
    channel: str,
    target_id: int,
    memos: list[CareMemo],
) -> tuple[list[str], list[tuple[CareMemo, Exception]]]:
    if channel == NotificationChannel.VK.value:
        return _send_vk(db, target_id, memos)
    if channel == NotificationChannel.MAX.value:
        return _send_max(db, target_id, memos)
    if channel == NotificationChannel.TELEGRAM.value:
        return _send_telegram(db, target_id, memos)
    return [], [(memo, RuntimeError(f"канал {channel}")) for memo in memos]


def _send_vk(
    db: Session,
    peer_id: int,
    memos: list[CareMemo],
) -> tuple[list[str], list[tuple[CareMemo, Exception]]]:
    ready: list[tuple[CareMemo, str]] = []
    errors: list[tuple[CareMemo, Exception]] = []
    for memo in memos:
        try:
            ready.append((memo, ensure_vk_attachment(db, memo, peer_id)))
        except Exception as exc:
            errors.append((memo, exc))
    if ready:
        try:
            _vk_messages_send(peer_id, [att for _, att in ready])
        except Exception as exc:
            errors.extend((memo, exc) for memo, _ in ready)
            ready = []
    return [memo.name for memo, _ in ready], errors


def _send_max(
    db: Session,
    user_id: int,
    memos: list[CareMemo],
) -> tuple[list[str], list[tuple[CareMemo, Exception]]]:
    ready: list[tuple[CareMemo, str]] = []
    errors: list[tuple[CareMemo, Exception]] = []
    for memo in memos:
        try:
            ready.append((memo, ensure_max_token(db, memo)))
        except Exception as exc:
            errors.append((memo, exc))
    if ready:
        try:
            _max_message(user_id, [token for _, token in ready])
        except Exception as exc:
            errors.extend((memo, exc) for memo, _ in ready)
            ready = []
    return [memo.name for memo, _ in ready], errors


def _send_telegram(
    db: Session,
    chat_id: int,
    memos: list[CareMemo],
) -> tuple[list[str], list[tuple[CareMemo, Exception]]]:
    if len(memos) > 1 and all(memo.telegram_file_id for memo in memos):
        try:
            _telegram_media_group(chat_id, [str(memo.telegram_file_id) for memo in memos])
        except Exception as exc:
            return [], [(memo, exc) for memo in memos]
        return [memo.name for memo in memos], []
    names: list[str] = []
    errors: list[tuple[CareMemo, Exception]] = []
    for memo in memos:
        try:
            _telegram_photo(db, chat_id, memo)
            names.append(memo.name)
        except Exception as exc:
            errors.append((memo, exc))
    return names, errors


def ensure_vk_attachment(db: Session, memo: CareMemo, peer_id: int) -> str:
    cached = (memo.vk_attachment or "").strip()
    if cached:
        return cached
    upload = _vk_call("photos.getMessagesUploadServer", {"peer_id": str(int(peer_id))})
    if not isinstance(upload, dict) or not upload.get("upload_url"):
        raise RuntimeError("VK: нет upload_url")
    saved = _post_file(str(upload["upload_url"]), "photo", memo)
    photos = _vk_call(
        "photos.saveMessagesPhoto",
        {
            "server": str(saved.get("server", "")),
            "photo": str(saved.get("photo", "")),
            "hash": str(saved.get("hash", "")),
        },
    )
    if isinstance(photos, dict):
        photos = [photos]
    if not isinstance(photos, list) or not photos or not isinstance(photos[0], dict):
        raise RuntimeError("VK: пустой saveMessagesPhoto")
    owner = photos[0].get("owner_id")
    photo_id = photos[0].get("id")
    if owner is None or photo_id is None:
        raise RuntimeError("VK: нет id фото")
    attachment = f"photo{owner}_{photo_id}"
    memo.vk_attachment = attachment
    db.flush()
    return attachment


def ensure_max_token(db: Session, memo: CareMemo) -> str:
    cached = (memo.max_image_token or "").strip()
    if cached:
        return cached
    settings = get_settings()
    token = (settings.max_bot_token or "").strip()
    if not token:
        raise RuntimeError("Max не настроен")
    base = (settings.max_api_base or "https://platform-api2.max.ru").rstrip("/")
    meta = _read_json(
        f"{base}/uploads?type=image",
        method="POST",
        data=b"",
        headers={"Authorization": token},
        use_max_ca=True,
    )
    found = _find_token(meta)
    if not found:
        url = str(meta.get("url") or "") if isinstance(meta, dict) else ""
        if not url:
            raise RuntimeError("Max: нет адреса загрузки")
        uploaded = _post_file(url, "data", memo, use_max_ca=True)
        found = _find_token(uploaded)
    if not found:
        raise RuntimeError("Max: нет токена картинки")
    memo.max_image_token = found
    db.flush()
    return found


def _telegram_photo(db: Session, chat_id: int, memo: CareMemo) -> None:
    cached = (memo.telegram_file_id or "").strip()
    if cached:
        _telegram_call("sendPhoto", {"chat_id": str(int(chat_id)), "photo": cached})
        return
    data = _telegram_upload(chat_id, memo)
    photos = []
    if isinstance(data, dict):
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        photos = result.get("photo") if isinstance(result.get("photo"), list) else []
    file_id = ""
    if photos and isinstance(photos[-1], dict):
        file_id = str(photos[-1].get("file_id") or "")
    if file_id:
        memo.telegram_file_id = file_id
        db.flush()


def _telegram_media_group(chat_id: int, file_ids: list[str]) -> None:
    media = [{"type": "photo", "media": fid} for fid in file_ids]
    _telegram_call(
        "sendMediaGroup",
        {"chat_id": str(int(chat_id)), "media": json.dumps(media, ensure_ascii=False)},
    )


def _vk_messages_send(peer_id: int, attachments: list[str]) -> None:
    _vk_call(
        "messages.send",
        {
            "peer_id": str(int(peer_id)),
            "attachment": ",".join(attachments),
            "random_id": str(random.randint(1, 2_147_483_647)),
            "message": "",
        },
    )


def _max_message(user_id: int, tokens: list[str]) -> None:
    settings = get_settings()
    token = (settings.max_bot_token or "").strip()
    if not token:
        raise RuntimeError("Max не настроен")
    base = (settings.max_api_base or "https://platform-api2.max.ru").rstrip("/")
    body = {
        "attachments": [{"type": "image", "payload": {"token": item}} for item in tokens],
    }
    _read_json(
        f"{base}/messages?user_id={int(user_id)}",
        method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": token, "Content-Type": "application/json; charset=utf-8"},
        use_max_ca=True,
    )


def _vk_call(method: str, params: dict[str, str]) -> Any:
    settings = get_settings()
    token = (settings.vk_group_token or "").strip()
    if not token:
        raise RuntimeError("VK не настроен")
    query = dict(params)
    query["access_token"] = token
    query["v"] = settings.vk_api_version or "5.199"
    url = "https://api.vk.com/method/" + method + "?" + urllib.parse.urlencode(query)
    data = _read_json(url, method="GET")
    if isinstance(data, dict) and "error" in data:
        from app.notifications import format_vk_api_error

        raise RuntimeError(format_vk_api_error(data["error"]))
    if isinstance(data, dict) and "response" in data:
        return data["response"]
    return data


def _telegram_call(method: str, fields: dict[str, str]) -> dict[str, Any]:
    settings = get_settings()
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    base = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/{method}"
    body = urllib.parse.urlencode(fields).encode("utf-8")
    data = _read_json(url, method="POST", data=body)
    if not isinstance(data, dict) or not data.get("ok"):
        desc = data.get("description") if isinstance(data, dict) else ""
        raise RuntimeError(desc or "Telegram API error")
    return data


def _telegram_upload(chat_id: int, memo: CareMemo) -> dict[str, Any]:
    settings = get_settings()
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    base = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/sendPhoto"
    raw, content_type = _multipart(
        {"chat_id": str(int(chat_id))},
        "photo",
        memo.file_name or "memo.jpg",
        _file_bytes(memo),
        memo.mime or "image/jpeg",
    )
    data = _read_json(url, method="POST", data=raw, headers={"Content-Type": content_type})
    if not isinstance(data, dict) or not data.get("ok"):
        desc = data.get("description") if isinstance(data, dict) else ""
        raise RuntimeError(desc or "Telegram API error")
    return data


def _post_file(url: str, field: str, memo: CareMemo, *, use_max_ca: bool = False) -> dict[str, Any]:
    raw, content_type = _multipart(
        {},
        field,
        memo.file_name or "memo.jpg",
        _file_bytes(memo),
        memo.mime or "image/jpeg",
    )
    data = _read_json(
        url,
        method="POST",
        data=raw,
        headers={"Content-Type": content_type},
        use_max_ca=use_max_ca,
    )
    return data if isinstance(data, dict) else {}


def _file_bytes(memo: CareMemo) -> bytes:
    root = media_root_dir().resolve()
    path = (root / (memo.file_path or "")).resolve()
    try:
        path.relative_to(root)
    except ValueError as e:
        raise RuntimeError("Файл памятки не найден") from e
    if not path.is_file():
        raise RuntimeError("Файл памятки не найден")
    return path.read_bytes()


def _multipart(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    mime: str,
) -> tuple[bytes, str]:
    boundary = "----lbcare" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    safe_name = filename.replace('"', "")
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _read_json(
    url: str,
    *,
    method: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    use_max_ca: bool = False,
) -> Any:
    if use_max_ca:
        import httpx

        from app.max_http import max_request

        try:
            status, raw_bytes = max_request(
                method,
                url,
                content=data,
                headers=headers,
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise RuntimeError(f"сеть: {e}") from e
        raw = raw_bytes.decode("utf-8", errors="replace")
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {raw[:300]}")
    else:
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"сеть: {e.reason}") from e
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("некорректный JSON") from e


def _find_token(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("token", "file_id", "fileId"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _find_token(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_token(value)
            if found:
                return found
    return ""


def _write_error(db: Session, parent: NotificationOutbox, memo: CareMemo, exc: Exception) -> None:
    text = f"Памятка {memo.name} — ошибка"
    detail = str(exc).strip()[:500] or text
    key = f"cmemoerr:{int(parent.id)}:{int(memo.id)}"
    err = NotificationOutbox(
        user_id=None,
        client_id=parent.client_id,
        target_kind=parent.target_kind or "client",
        booking_id=parent.booking_id,
        visit_id=parent.visit_id,
        channel=parent.channel,
        event_type=EVENT_CARE_MEMO_ERROR,
        payload_json=json.dumps(
            {"text": text, "target_kind": parent.target_kind or "client", "care_memo_id": int(memo.id)},
            ensure_ascii=False,
        ),
        body=text,
        status=NotificationOutboxStatus.FAILED,
        error=detail,
        dedupe_key=key,
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


def _payload(row: NotificationOutbox) -> dict[str, Any]:
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
