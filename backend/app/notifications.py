"""Уведомления мастерам о бронях (VK / Max / Telegram): текст, outbox, отправка."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Booking,
    BookingKind,
    BookingPlannedService,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
)
from app.display_time import DEFAULT_DISPLAY_TIMEZONE, format_naive_utc_datetime, get_display_timezone
from app.settings import get_settings
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

EVENT_BOOKING_CREATED = "booking_created"
EVENT_BOOKING_UPDATED = "booking_updated"
EVENT_BOOKING_CANCELLED = "booking_cancelled"

_EVENT_LABEL_RU = {
    EVENT_BOOKING_CREATED: "создана",
    EVENT_BOOKING_UPDATED: "изменена",
    EVENT_BOOKING_CANCELLED: "отменена",
}

DEFAULT_OUTBOX_LIMIT = 50
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_SENDING_TIMEOUT_SEC = 120
TELEGRAM_HTTP_TIMEOUT_SEC = 5
VK_HTTP_TIMEOUT_SEC = 5


def _booking_kind_label(kind: BookingKind | str | None) -> str:
    raw = kind.value if isinstance(kind, BookingKind) else (kind or "")
    if raw == BookingKind.VISIT.value:
        return "Визит"
    if raw == BookingKind.PRODUCT_SALE.value:
        return "Продажа"
    if raw == BookingKind.CONSULTATION.value:
        return "Консультация"
    return raw or "—"


def _service_name_list(booking: Booking) -> list[str]:
    parts: list[str] = []
    if booking.planned_service is not None:
        name = (booking.planned_service.name or "").strip()
        if name:
            parts.append(name)
    for ps in booking.planned_services or []:
        svc = ps.service
        name = (svc.name if svc is not None else "") or ""
        name = name.strip()
        if name and name not in parts:
            parts.append(name)
    if parts:
        return parts
    if booking.planned_product_kind:
        return [f"товар: {booking.planned_product_kind}"]
    return []


def _service_label(booking: Booking) -> str:
    parts = _service_name_list(booking)
    return ", ".join(parts) if parts else "—"


def _service_line_bits(booking: Booking) -> list[str]:
    bits: list[str] = []
    for ps in sorted(booking.planned_services or [], key=lambda x: (int(x.sort_order or 0), int(x.id or 0))):
        mids = sorted(int(m.master_id) for m in (ps.masters or []) if m.master_id is not None)
        bits.append(
            f"{int(ps.service_id)}:{ps.planned_start_time}:{ps.duration_minutes}:{','.join(map(str, mids))}"
        )
    return bits


def booking_planned_master_user_ids(booking: Booking) -> list[int]:
    """Id мастеров, назначенных на бронь (visit-уровень + по услугам)."""
    ids: set[int] = set()
    for bm in booking.masters or []:
        if bm.master_id is not None:
            ids.add(int(bm.master_id))
    for ps in booking.planned_services or []:
        for m in ps.masters or []:
            if m.master_id is not None:
                ids.add(int(m.master_id))
    return sorted(ids)


def _clip_notify_text(value: str, limit: int = 100) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _master_display_name(user: User | None, user_id: int) -> str:
    if user is None:
        return str(user_id)
    return (user.display_name or user.username or str(user_id)).strip() or str(user_id)


def capture_booking_notify_snapshot(db: Session, booking: Booking) -> dict[str, Any]:
    """Снимок значимых для уведомления полей (тот же набор, что в dedupe content-hash)."""
    booking = _ensure_booking_loaded(db, booking)
    master_ids = booking_planned_master_user_ids(booking)
    names: dict[str, str] = {}
    if master_ids:
        for u in db.scalars(select(User).where(User.id.in_(master_ids))).all():
            names[str(int(u.id))] = _master_display_name(u, int(u.id))
    client_name = "—"
    if booking.client is not None:
        client_name = (booking.client.name or "").strip() or "—"
    return {
        "planned_date": booking.planned_date.isoformat(timespec="seconds") if booking.planned_date else "",
        "client_id": int(booking.client_id) if booking.client_id is not None else 0,
        "client_name": client_name,
        "kind": booking.kind.value if booking.kind else "",
        "planned_service_id": int(booking.planned_service_id) if booking.planned_service_id else 0,
        "planned_product_kind": (booking.planned_product_kind or "").strip(),
        "comment": (booking.comment or "").strip(),
        "master_ids": master_ids,
        "master_names": names,
        "service_names": _service_name_list(booking),
        "service_lines": _service_line_bits(booking),
    }


def booking_content_version_from_snapshot(snapshot: dict[str, Any]) -> str:
    """Хеш снимка значимых полей — версия для dedupe_key."""
    raw = "|".join(
        [
            str(snapshot.get("planned_date") or ""),
            str(snapshot.get("client_id") or ""),
            str(snapshot.get("kind") or ""),
            str(snapshot.get("planned_service_id") or ""),
            str(snapshot.get("planned_product_kind") or ""),
            ",".join(str(x) for x in (snapshot.get("master_ids") or [])),
            ";".join(str(x) for x in (snapshot.get("service_lines") or [])),
            str(snapshot.get("comment") or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def booking_content_version(booking: Booking) -> str:
    """Хеш значимых полей брони — для dedupe без ложных дублей при повторном сохранении."""
    return booking_content_version_from_snapshot(
        {
            "planned_date": booking.planned_date.isoformat(timespec="seconds") if booking.planned_date else "",
            "client_id": int(booking.client_id) if booking.client_id is not None else 0,
            "kind": booking.kind.value if booking.kind else "",
            "planned_service_id": int(booking.planned_service_id) if booking.planned_service_id else 0,
            "planned_product_kind": (booking.planned_product_kind or "").strip(),
            "comment": (booking.comment or "").strip(),
            "master_ids": booking_planned_master_user_ids(booking),
            "service_lines": _service_line_bits(booking),
        }
    )


def _format_snapshot_when(snapshot: dict[str, Any], tz_name: str) -> str:
    raw = str(snapshot.get("planned_date") or "").strip()
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return format_naive_utc_datetime(dt, tz_name, "%d.%m.%Y %H:%M") or "—"


def diff_booking_notify_snapshots(
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    tz_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> list[str]:
    """Строки блока «Что изменилось» (без заголовка). Пусто — значимых изменений нет."""
    lines: list[str] = []

    old_when = _format_snapshot_when(old, tz_name)
    new_when = _format_snapshot_when(new, tz_name)
    if old_when != new_when:
        lines.append(f"Дата/время: {old_when} → {new_when}")

    old_client = str(old.get("client_name") or "—")
    new_client = str(new.get("client_name") or "—")
    if int(old.get("client_id") or 0) != int(new.get("client_id") or 0) or old_client != new_client:
        lines.append(f"Клиент: {old_client} → {new_client}")

    old_kind = _booking_kind_label(str(old.get("kind") or ""))
    new_kind = _booking_kind_label(str(new.get("kind") or ""))
    if str(old.get("kind") or "") != str(new.get("kind") or ""):
        lines.append(f"Тип: {old_kind} → {new_kind}")

    old_services = [str(x) for x in (old.get("service_names") or [])]
    new_services = [str(x) for x in (new.get("service_names") or [])]
    old_set = set(old_services)
    new_set = set(new_services)
    removed_svc = sorted(old_set - new_set)
    added_svc = sorted(new_set - old_set)
    if removed_svc or added_svc:
        for name in removed_svc:
            lines.append(f"− Услуга {name}")
        for name in added_svc:
            lines.append(f"+ Услуга {name}")
    elif list(old.get("service_lines") or []) != list(new.get("service_lines") or []):
        lines.append("Услуга: изменено время или длительность")

    old_comment = str(old.get("comment") or "").strip()
    new_comment = str(new.get("comment") or "").strip()
    if old_comment != new_comment:
        lines.append(
            "Комментарий: изменён "
            f"({_clip_notify_text(old_comment) or '—'} → {_clip_notify_text(new_comment) or '—'})"
        )

    old_mids = [int(x) for x in (old.get("master_ids") or [])]
    new_mids = [int(x) for x in (new.get("master_ids") or [])]
    old_mset = set(old_mids)
    new_mset = set(new_mids)
    added_m = sorted(new_mset - old_mset)
    removed_m = sorted(old_mset - new_mset)
    if added_m or removed_m:
        old_names = {int(k): str(v) for k, v in (old.get("master_names") or {}).items()}
        new_names = {int(k): str(v) for k, v in (new.get("master_names") or {}).items()}
        bits: list[str] = []
        for mid in added_m:
            bits.append(f"добавлена {new_names.get(mid) or mid}")
        for mid in removed_m:
            bits.append(f"снята {old_names.get(mid) or mid}")
        lines.append("Мастера: " + " / ".join(bits))

    return lines


def build_booking_master_message(
    booking: Booking,
    event_type: str,
    *,
    tz_name: str = DEFAULT_DISPLAY_TIMEZONE,
    unassigned: bool = False,
    change_lines: Sequence[str] | None = None,
) -> str:
    """Простой текст уведомления мастеру о брони (без HTML)."""
    when = format_naive_utc_datetime(booking.planned_date, tz_name, "%d.%m.%Y %H:%M") or "—"
    client_name = "—"
    if booking.client is not None:
        client_name = (booking.client.name or "").strip() or "—"
    kind_ru = _booking_kind_label(booking.kind)
    service = _service_label(booking)
    comment = (booking.comment or "").strip()

    if unassigned:
        headline = f"Бронь #{booking.id} снята с вас"
    else:
        event_ru = _EVENT_LABEL_RU.get(event_type, event_type)
        headline = f"Бронь #{booking.id} {event_ru}"

    lines = [headline]
    if change_lines:
        lines.append("Что изменилось:")
        for item in change_lines:
            lines.append(f"• {item}")
    lines.extend(
        [
            f"Дата/время: {when}",
            f"Клиент: {client_name}",
            f"Тип: {kind_ru}",
            f"Услуга: {service}",
        ]
    )
    if comment:
        lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)


def _dedupe_key(
    *,
    event_type: str,
    booking_id: int,
    user_id: int,
    channel: NotificationChannel,
    version: str,
) -> str:
    return f"{event_type}:{booking_id}:{user_id}:{channel.value}:{version}"


def _event_version(booking: Booking, event_type: str, *, version_override: str | None = None) -> str:
    if version_override:
        return version_override
    content = booking_content_version(booking)
    if event_type == EVENT_BOOKING_CANCELLED:
        stamp = booking.cancelled_at or booking.updated_at or utcnow_naive()
        return f"cancel:{stamp.isoformat(timespec='seconds')}:{content}"
    if event_type == EVENT_BOOKING_CREATED:
        return f"create:{content}"
    return f"upd:{content}"


def _ensure_booking_loaded(db: Session, booking: Booking) -> Booking:
    """Подгрузить связи, нужные для текста и списка мастеров."""
    bid = int(booking.id)
    row = db.scalars(
        select(Booking)
        .where(Booking.id == bid)
        .options(
            selectinload(Booking.client),
            selectinload(Booking.planned_service),
            selectinload(Booking.masters),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.masters),
        )
    ).first()
    return row if row is not None else booking


def enqueue_master_booking_notifications(
    db: Session,
    booking: Booking,
    event_type: str,
    *,
    master_user_ids: Sequence[int] | None = None,
    text_override: str | None = None,
    version_override: str | None = None,
    channels: Iterable[NotificationChannel] | None = None,
    unassigned: bool = False,
    change_lines: Sequence[str] | None = None,
) -> list[NotificationOutbox]:
    """Поставить в outbox уведомления назначенным мастерам, с dedupe.

    По умолчанию — все мастера брони и каналы TG+VK (у кого есть id).
    """
    booking = _ensure_booking_loaded(db, booking)
    if master_user_ids is None:
        master_ids = booking_planned_master_user_ids(booking)
    else:
        master_ids = sorted({int(x) for x in master_user_ids if int(x) > 0})
    if not master_ids:
        return []

    allowed_channels = (
        set(channels)
        if channels is not None
        else {NotificationChannel.TELEGRAM, NotificationChannel.VK, NotificationChannel.MAX}
    )
    tz_name = get_display_timezone(db)
    text = text_override or build_booking_master_message(
        booking,
        event_type,
        tz_name=tz_name,
        unassigned=unassigned,
        change_lines=change_lines,
    )
    version = _event_version(booking, event_type, version_override=version_override)
    created: list[NotificationOutbox] = []

    for uid in master_ids:
        user = db.get(User, uid)
        if user is None:
            continue
        from app.notify_prefs import user_wants_booking_notifications

        if not user_wants_booking_notifications(user):
            continue

        channel_targets: list[tuple[NotificationChannel, int]] = []
        if NotificationChannel.VK in allowed_channels and user.vk_user_id is not None:
            channel_targets.append((NotificationChannel.VK, int(user.vk_user_id)))
        if NotificationChannel.MAX in allowed_channels and user.max_user_id is not None:
            channel_targets.append((NotificationChannel.MAX, int(user.max_user_id)))
        if NotificationChannel.TELEGRAM in allowed_channels and user.telegram_chat_id is not None:
            channel_targets.append((NotificationChannel.TELEGRAM, int(user.telegram_chat_id)))
        if not channel_targets:
            continue

        for channel, target_id in channel_targets:
            key = _dedupe_key(
                event_type=event_type,
                booking_id=int(booking.id),
                user_id=uid,
                channel=channel,
                version=version,
            )
            exists = db.scalar(
                select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)
            )
            if exists is not None:
                continue
            payload = {
                "text": text,
                "target_id": target_id,
                "event_type": event_type,
                "booking_id": int(booking.id),
            }
            row = NotificationOutbox(
                user_id=uid,
                booking_id=int(booking.id),
                channel=channel,
                event_type=event_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
                status=NotificationOutboxStatus.PENDING,
                error=None,
                dedupe_key=key,
                attempt_count=0,
                created_at=utcnow_naive(),
                sent_at=None,
            )
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
            except IntegrityError:
                continue
            created.append(row)

    return created


def send_telegram(chat_id: int, text: str) -> None:
    """Отправить сообщение через Telegram Bot API. При ошибке — исключение."""
    settings = get_settings()
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    base = (settings.telegram_api_base or "https://api.telegram.org").rstrip("/")
    url = f"{base}/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "1",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TELEGRAM_HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"Telegram HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Telegram сеть: {e.reason}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Telegram: некорректный JSON ответа: {raw[:200]}") from e
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or f"Telegram API error: {raw[:200]}")


MAX_HTTP_TIMEOUT_SEC = 8


def send_max(
    user_id: int | None = None,
    text: str = "",
    *,
    chat_id: int | None = None,
) -> None:
    """Отправка в Max: user_id (личка) или chat_id (группа). Без токена — «Max не настроен»."""
    settings = get_settings()
    token = (settings.max_bot_token or "").strip()
    if not token:
        raise RuntimeError("Max не настроен")
    if chat_id is not None:
        qs = f"chat_id={int(chat_id)}"
    elif user_id is not None:
        qs = f"user_id={int(user_id)}"
    else:
        raise RuntimeError("Max: нужен user_id или chat_id")
    base = (settings.max_api_base or "https://platform-api2.max.ru").rstrip("/")
    url = f"{base}/messages?{qs}"
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": token,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=MAX_HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"Max HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Max сеть: {e.reason}") from e

    if not raw.strip():
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data.get("message") or data.get("error") or raw[:200]))
    if isinstance(data, dict) and "error" in data and data.get("code") not in (None, 0, "ok"):
        raise RuntimeError(str(data.get("error") or raw[:200]))


def send_vk(
    user_id: int | None = None,
    text: str = "",
    *,
    peer_id: int | None = None,
) -> None:
    """Отправка во VK (messages.send): user_id или peer_id беседы."""
    settings = get_settings()
    token = (settings.vk_group_token or "").strip()
    if not token:
        raise RuntimeError("VK не настроен")
    params: dict[str, str] = {
        "message": text,
        "random_id": str(random.randint(1, 2_147_483_647)),
        "access_token": token,
        "v": settings.vk_api_version or "5.199",
    }
    if peer_id is not None:
        params["peer_id"] = str(int(peer_id))
    elif user_id is not None:
        params["user_id"] = str(int(user_id))
    else:
        raise RuntimeError("VK: нужен user_id или peer_id")
    # Не логируем URL с access_token.
    url = "https://api.vk.com/method/messages.send?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=VK_HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        detail = _redact_secrets(detail)
        raise RuntimeError(f"VK HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"VK сеть: {e.reason}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"VK: некорректный JSON ответа: {_redact_secrets(raw)[:200]}") from e
    if "error" in data:
        raise RuntimeError(format_vk_api_error(data["error"]))


def _redact_secrets(text: str) -> str:
    """Убрать возможные токены из текста ошибок перед логом/исключением."""
    return re.sub(r"(access_token=)[^&\s]+", r"\1***", text or "", flags=re.I)


def format_vk_api_error(err: Any) -> str:
    """Человекочитаемая ошибка VK API (без токена)."""
    if not isinstance(err, dict):
        return f"VK API: {_redact_secrets(str(err))[:300]}"
    code = err.get("error_code")
    msg = str(err.get("error_msg") or "").strip() or "ошибка"
    msg = _redact_secrets(msg)
    hint = VK_ERROR_HINTS.get(int(code)) if code is not None else None
    if code is not None and hint:
        return f"VK API {code}: {msg} — {hint}"
    if code is not None:
        return f"VK API {code}: {msg}"
    return f"VK API: {msg}"


def explain_outbox_error(error: str | None, *, channel: str | None = None) -> str:
    """Краткая расшифровка для админ-журнала outbox."""
    raw = (error or "").strip()
    if not raw:
        return ""
    ch = (channel or "").lower()
    if ch == "vk" or "VK API" in raw or raw.startswith("VK "):
        m = re.search(r"VK API\s+(\d+)", raw)
        if m:
            code = int(m.group(1))
            hint = VK_ERROR_HINTS.get(code)
            if hint and hint not in raw:
                return f"{raw} ({hint})"
        if "901" in raw and "разрешен" not in raw.lower():
            return f"{raw} ({VK_ERROR_HINTS[901]})"
        if "902" in raw and "приватн" not in raw.lower():
            return f"{raw} ({VK_ERROR_HINTS[902]})"
    return raw


VK_ERROR_HINTS: dict[int, str] = {
    901: "нельзя писать пользователю без разрешения (нужно «Разрешить сообщения»)",
    902: "пользователь ограничил сообщения из‑за настроек приватности",
    7: "нет прав у ключа сообщества",
    15: "доступ запрещён",
    100: "неверный запрос (проверьте user_id / токен)",
    900: "нельзя отправить сообщение этому пользователю",
}

def _parse_payload(row: NotificationOutbox) -> dict[str, Any]:
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _send_outbox_row(row: NotificationOutbox) -> None:
    payload = _parse_payload(row)
    text = str(payload.get("text") or "").strip()
    if not text:
        raise RuntimeError("Пустой текст в payload_json")
    target = payload.get("target_id")
    if target is None:
        raise RuntimeError("Нет target_id в payload_json")
    target_id = int(target)
    kind = (payload.get("target_kind") or row.target_kind or "user").strip().lower()
    if row.channel == NotificationChannel.TELEGRAM:
        send_telegram(target_id, text)
    elif row.channel == NotificationChannel.VK:
        if kind == "admin_chat":
            send_vk(text=text, peer_id=target_id)
        else:
            send_vk(target_id, text)
    elif row.channel == NotificationChannel.MAX:
        if kind == "admin_chat":
            send_max(text=text, chat_id=target_id)
        else:
            send_max(target_id, text)
    else:
        raise RuntimeError(f"Неизвестный канал: {row.channel}")


def _record_client_send_if_needed(db: Session, row: NotificationOutbox) -> None:
    """После успешной отправки клиенту — строка в client_notification_sends."""
    if (row.target_kind or "user") != "client" or row.client_id is None:
        return
    if row.event_type not in (
        "client_booking_reminder",
        "client_after_booking",
    ):
        return
    payload = _parse_payload(row)
    target = payload.get("target_id")
    if target is None:
        return
    from app.db.models import ClientNotificationSend

    db.add(
        ClientNotificationSend(
            client_id=int(row.client_id),
            booking_id=int(row.booking_id) if row.booking_id is not None else 0,
            channel=row.channel.value if hasattr(row.channel, "value") else str(row.channel),
            event_type=str(row.event_type),
            rule_id=int(payload["rule_id"]) if payload.get("rule_id") is not None else None,
            outbox_id=int(row.id) if row.id is not None else None,
            target_id=int(target),
            sent_at=utcnow_naive(),
        )
    )


def _eligible_outbox_clause(
    *,
    max_attempts: int,
    stale_before,
    only_ids: Sequence[int] | None,
    force: bool,
):
    max_att = max(1, int(max_attempts))
    pending_or_failed = or_(
        NotificationOutbox.status == NotificationOutboxStatus.PENDING,
        and_(
            NotificationOutbox.status == NotificationOutboxStatus.FAILED,
            NotificationOutbox.attempt_count < max_att,
        ),
    )
    if force:
        pending_or_failed = NotificationOutbox.status.in_(
            (
                NotificationOutboxStatus.PENDING,
                NotificationOutboxStatus.FAILED,
                NotificationOutboxStatus.SENDING,
            )
        )
    stale_sending = and_(
        NotificationOutbox.status == NotificationOutboxStatus.SENDING,
        or_(
            NotificationOutbox.locked_at.is_(None),
            NotificationOutbox.locked_at < stale_before,
        ),
    )
    clause = or_(pending_or_failed, stale_sending)
    if only_ids is not None:
        clause = and_(clause, NotificationOutbox.id.in_([int(x) for x in only_ids]))
    return clause


def _claim_outbox_rows(
    db: Session,
    *,
    limit: int,
    max_attempts: int,
    only_ids: Sequence[int] | None,
    force: bool,
    sending_timeout_sec: int,
) -> list[int]:
    """Захватить строки (status=sending). Postgres: FOR UPDATE SKIP LOCKED."""
    now = utcnow_naive()
    stale_before = now - timedelta(seconds=max(30, int(sending_timeout_sec)))
    lim = max(1, int(limit))
    clause = _eligible_outbox_clause(
        max_attempts=max_attempts,
        stale_before=stale_before,
        only_ids=only_ids,
        force=force,
    )
    stmt = (
        select(NotificationOutbox)
        .where(clause)
        .order_by(NotificationOutbox.id.asc())
        .limit(lim)
    )
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    rows = list(db.scalars(stmt).all())
    claimed_ids: list[int] = []
    for row in rows:
        if row.status == NotificationOutboxStatus.SENT:
            continue
        if (
            not force
            and row.status == NotificationOutboxStatus.FAILED
            and int(row.attempt_count or 0) >= max(1, int(max_attempts))
        ):
            continue
        if (
            row.status == NotificationOutboxStatus.SENDING
            and row.locked_at is not None
            and row.locked_at >= stale_before
            and not force
        ):
            continue
        row.status = NotificationOutboxStatus.SENDING
        row.locked_at = now
        claimed_ids.append(int(row.id))
    if claimed_ids:
        db.flush()
        db.commit()
    return claimed_ids


def process_outbox(
    db: Session,
    *,
    limit: int = DEFAULT_OUTBOX_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    only_ids: Sequence[int] | None = None,
    force: bool = False,
    sending_timeout_sec: int = DEFAULT_SENDING_TIMEOUT_SEC,
) -> dict[str, int]:
    """Обработать pending/failed (и просроченный sending). Возвращает счётчики.

    Захват через status=sending + locked_at (на Postgres — FOR UPDATE SKIP LOCKED),
    чтобы несколько процессов не отправили одну запись дважды.

    force=True + only_ids: обработать указанные id даже если attempt_count уже на лимите
    (ручной «Повторить» в админке).
    """
    if only_ids is not None and not list(only_ids):
        return {"processed": 0, "sent": 0, "failed": 0}

    claimed_ids = _claim_outbox_rows(
        db,
        limit=limit,
        max_attempts=max_attempts,
        only_ids=only_ids,
        force=force,
        sending_timeout_sec=sending_timeout_sec,
    )
    stats = {"processed": 0, "sent": 0, "failed": 0}
    for cid in claimed_ids:
        row = db.get(NotificationOutbox, cid)
        if row is None or row.status != NotificationOutboxStatus.SENDING:
            continue
        stats["processed"] += 1
        row.attempt_count = int(row.attempt_count or 0) + 1
        try:
            _send_outbox_row(row)
        except Exception as e:
            row.status = NotificationOutboxStatus.FAILED
            row.error = str(e)[:2000]
            row.locked_at = None
            stats["failed"] += 1
            logger.warning("notification_outbox #%s failed: %s", row.id, e)
            db.commit()
            continue
        row.status = NotificationOutboxStatus.SENT
        row.sent_at = utcnow_naive()
        row.error = None
        row.locked_at = None
        try:
            _record_client_send_if_needed(db, row)
        except Exception:
            logger.exception("notification_outbox #%s: client send log failed", row.id)
        stats["sent"] += 1
        db.commit()
    return stats


def list_recent_notification_outbox(db: Session, *, limit: int = 50) -> list[NotificationOutbox]:
    lim = max(1, min(int(limit), 200))
    return list(
        db.scalars(
            select(NotificationOutbox)
            .options(
                selectinload(NotificationOutbox.user),
                selectinload(NotificationOutbox.booking),
            )
            .order_by(NotificationOutbox.id.desc())
            .limit(lim)
        ).all()
    )


def retry_notification_outbox_entry(db: Session, entry_id: int) -> NotificationOutbox:
    """Принудительно повторить failed/pending/sending запись (для кнопки в админке)."""
    row = db.get(NotificationOutbox, int(entry_id))
    if row is None:
        raise ValueError("Запись outbox не найдена.")
    if row.status == NotificationOutboxStatus.SENT:
        raise ValueError("Уже отправлено — повтор не нужен.")
    if row.status not in (
        NotificationOutboxStatus.PENDING,
        NotificationOutboxStatus.FAILED,
        NotificationOutboxStatus.SENDING,
    ):
        raise ValueError(f"Нельзя повторить статус {row.status}.")
    row.status = NotificationOutboxStatus.PENDING
    row.locked_at = None
    db.flush()
    process_outbox(db, limit=1, only_ids=[int(row.id)], force=True)
    db.refresh(row)
    return row