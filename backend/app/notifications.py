"""Уведомления мастерам о бронях (Telegram / VK): текст, outbox, отправка."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
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


def _service_label(booking: Booking) -> str:
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
        return ", ".join(parts)
    if booking.planned_product_kind:
        return f"товар: {booking.planned_product_kind}"
    return "—"


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


def booking_content_version(booking: Booking) -> str:
    """Хеш значимых полей брони — для dedupe без ложных дублей при повторном сохранении."""
    svc_bits: list[str] = []
    for ps in sorted(booking.planned_services or [], key=lambda x: (int(x.sort_order or 0), int(x.id or 0))):
        mids = sorted(int(m.master_id) for m in (ps.masters or []) if m.master_id is not None)
        svc_bits.append(
            f"{int(ps.service_id)}:{ps.planned_start_time}:{ps.duration_minutes}:{','.join(map(str, mids))}"
        )
    raw = "|".join(
        [
            str(booking.planned_date),
            str(booking.status.value if booking.status else ""),
            str(booking.kind.value if booking.kind else ""),
            str(booking.planned_service_id or ""),
            str(booking.planned_product_kind or ""),
            ",".join(map(str, booking_planned_master_user_ids(booking))),
            ";".join(svc_bits),
            (booking.comment or "").strip(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_booking_master_message(
    booking: Booking,
    event_type: str,
    *,
    tz_name: str = DEFAULT_DISPLAY_TIMEZONE,
    unassigned: bool = False,
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

    lines = [
        headline,
        f"Дата/время: {when}",
        f"Клиент: {client_name}",
        f"Тип: {kind_ru}",
        f"Услуга: {service}",
    ]
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
        set(channels) if channels is not None else {NotificationChannel.TELEGRAM, NotificationChannel.VK}
    )
    tz_name = get_display_timezone(db)
    text = text_override or build_booking_master_message(
        booking, event_type, tz_name=tz_name, unassigned=unassigned
    )
    version = _event_version(booking, event_type, version_override=version_override)
    created: list[NotificationOutbox] = []

    for uid in master_ids:
        user = db.get(User, uid)
        if user is None:
            continue
        if not bool(user.notify_enabled):
            continue

        channel_targets: list[tuple[NotificationChannel, int]] = []
        if NotificationChannel.TELEGRAM in allowed_channels and user.telegram_chat_id is not None:
            channel_targets.append((NotificationChannel.TELEGRAM, int(user.telegram_chat_id)))
        if NotificationChannel.VK in allowed_channels and user.vk_user_id is not None:
            channel_targets.append((NotificationChannel.VK, int(user.vk_user_id)))
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
    token = (get_settings().telegram_bot_token or "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
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


def send_vk(user_id: int, text: str) -> None:
    """Отправка во VK. Без токена — «VK не настроен»; с токеном — messages.send."""
    token = (get_settings().vk_group_token or "").strip()
    if not token:
        raise RuntimeError("VK не настроен")
    params = {
        "user_id": str(user_id),
        "message": text,
        "random_id": str(random.randint(1, 2_147_483_647)),
        "access_token": token,
        "v": "5.199",
    }
    url = "https://api.vk.com/method/messages.send?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=VK_HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"VK HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"VK сеть: {e.reason}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"VK: некорректный JSON ответа: {raw[:200]}") from e
    if "error" in data:
        err = data["error"]
        msg = err.get("error_msg") if isinstance(err, dict) else str(err)
        raise RuntimeError(f"VK API: {msg}")


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
    if row.channel == NotificationChannel.TELEGRAM:
        send_telegram(target_id, text)
    elif row.channel == NotificationChannel.VK:
        send_vk(target_id, text)
    else:
        raise RuntimeError(f"Неизвестный канал: {row.channel}")


def process_outbox(
    db: Session,
    *,
    limit: int = DEFAULT_OUTBOX_LIMIT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    only_ids: Sequence[int] | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Обработать pending и failed (с лимитом попыток). Возвращает счётчики.

    force=True + only_ids: обработать указанные id даже если attempt_count уже на лимите
    (ручной «Повторить» в админке).
    """
    lim = max(1, int(limit))
    max_att = max(1, int(max_attempts))
    if only_ids is not None:
        ids = [int(x) for x in only_ids]
        if not ids:
            return {"processed": 0, "sent": 0, "failed": 0}
        if force:
            stmt = select(NotificationOutbox).where(
                NotificationOutbox.id.in_(ids),
                NotificationOutbox.status.in_(
                    (NotificationOutboxStatus.PENDING, NotificationOutboxStatus.FAILED)
                ),
            )
        else:
            stmt = select(NotificationOutbox).where(
                NotificationOutbox.id.in_(ids),
                or_(
                    NotificationOutbox.status == NotificationOutboxStatus.PENDING,
                    and_(
                        NotificationOutbox.status == NotificationOutboxStatus.FAILED,
                        NotificationOutbox.attempt_count < max_att,
                    ),
                ),
            )
    else:
        stmt = select(NotificationOutbox).where(
            or_(
                NotificationOutbox.status == NotificationOutboxStatus.PENDING,
                and_(
                    NotificationOutbox.status == NotificationOutboxStatus.FAILED,
                    NotificationOutbox.attempt_count < max_att,
                ),
            )
        )
    rows = list(db.scalars(stmt.order_by(NotificationOutbox.id.asc()).limit(lim)).all())
    stats = {"processed": 0, "sent": 0, "failed": 0}
    for row in rows:
        stats["processed"] += 1
        row.attempt_count = int(row.attempt_count or 0) + 1
        try:
            _send_outbox_row(row)
        except Exception as e:
            row.status = NotificationOutboxStatus.FAILED
            row.error = str(e)[:2000]
            stats["failed"] += 1
            logger.warning("notification_outbox #%s failed: %s", row.id, e)
            continue
        row.status = NotificationOutboxStatus.SENT
        row.sent_at = utcnow_naive()
        row.error = None
        stats["sent"] += 1
    db.flush()
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
    """Принудительно повторить failed/pending запись (для кнопки в админке)."""
    row = db.get(NotificationOutbox, int(entry_id))
    if row is None:
        raise ValueError("Запись outbox не найдена.")
    if row.status == NotificationOutboxStatus.SENT:
        raise ValueError("Уже отправлено — повтор не нужен.")
    if row.status not in (NotificationOutboxStatus.PENDING, NotificationOutboxStatus.FAILED):
        raise ValueError(f"Нельзя повторить статус {row.status}.")
    row.status = NotificationOutboxStatus.PENDING
    db.flush()
    process_outbox(db, limit=1, only_ids=[int(row.id)], force=True)
    db.refresh(row)
    return row