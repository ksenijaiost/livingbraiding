"""Настраиваемые напоминания мастерам о предстоящих бронях."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.consultation_booking import OPEN_BOOKING_STATUSES
from app.db.models import (
    Booking,
    BookingPlannedService,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    User,
    UserReminderSetting,
)
from app.display_time import DEFAULT_DISPLAY_TIMEZONE, format_naive_utc_datetime, get_display_timezone
from app.notifications import (
    _booking_kind_label,
    _ensure_booking_loaded,
    _service_label,
    booking_planned_master_user_ids,
)
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

EVENT_BOOKING_REMINDER = "booking_reminder"

# Дефолт, пока пользователь не сохранял свои настройки.
DEFAULT_REMINDER_MINUTES: tuple[int, ...] = (24 * 60, 2 * 60)
MAX_REMINDERS_PER_USER = 5
MIN_REMINDER_HOURS = 0.5
MAX_REMINDER_HOURS = 72.0
# Не шлём напоминания, если момент срабатывания был раньше чем now − окно.
REMINDER_LATENESS_WINDOW = timedelta(minutes=10)


def hours_to_minutes(hours: float) -> int:
    return int(round(float(hours) * 60))


def minutes_to_hours_label(minutes: int) -> str:
    h = float(minutes) / 60.0
    if abs(h - round(h)) < 1e-9:
        return str(int(round(h)))
    text = f"{h:.2f}".rstrip("0").rstrip(".")
    return text


def parse_reminder_hours(raw: Any) -> float:
    """Разобрать часы; ValueError при невалидном значении."""
    if raw is None:
        raise ValueError("Укажите число часов.")
    s = str(raw).strip().replace(",", ".")
    if not s:
        raise ValueError("Укажите число часов.")
    try:
        hours = float(s)
    except ValueError as e:
        raise ValueError("Некорректное число часов.") from e
    if hours < MIN_REMINDER_HOURS or hours > MAX_REMINDER_HOURS:
        raise ValueError(f"Часы напоминания: от {MIN_REMINDER_HOURS} до {MAX_REMINDER_HOURS}.")
    minutes = hours_to_minutes(hours)
    if minutes <= 0:
        raise ValueError("Слишком малое значение.")
    # Обратная проверка после округления.
    back = minutes / 60.0
    if back < MIN_REMINDER_HOURS - 1e-9 or back > MAX_REMINDER_HOURS + 1e-9:
        raise ValueError(f"Часы напоминания: от {MIN_REMINDER_HOURS} до {MAX_REMINDER_HOURS}.")
    return minutes / 60.0


def get_effective_reminder_minutes(db: Session, user: User | int) -> list[int]:
    """Минуты до записи для пользователя (дефолт 24ч+2ч, если ещё не настраивал)."""
    if isinstance(user, int):
        u = db.get(User, int(user))
        if u is None:
            return list(DEFAULT_REMINDER_MINUTES)
        user = u
    if not bool(user.reminders_configured):
        return list(DEFAULT_REMINDER_MINUTES)
    rows = list(
        db.scalars(
            select(UserReminderSetting)
            .where(UserReminderSetting.user_id == int(user.id))
            .order_by(UserReminderSetting.position.asc(), UserReminderSetting.id.asc())
        ).all()
    )
    return [int(r.minutes_before) for r in rows]


def list_reminder_settings_for_ui(db: Session, user: User) -> list[dict[str, Any]]:
    minutes = get_effective_reminder_minutes(db, user)
    return [
        {
            "minutes_before": m,
            "hours_label": minutes_to_hours_label(m),
            "is_default": not bool(user.reminders_configured),
        }
        for m in minutes
    ]


def replace_user_reminder_minutes(db: Session, user: User, minutes_list: Sequence[int]) -> None:
    """Сохранить список напоминаний (уже в минутах). Помечает reminders_configured=True."""
    cleaned: list[int] = []
    seen: set[int] = set()
    for m in minutes_list:
        mi = int(m)
        if mi in seen:
            raise ValueError("Дубли значений напоминаний запрещены.")
        hours = mi / 60.0
        if hours < MIN_REMINDER_HOURS - 1e-9 or hours > MAX_REMINDER_HOURS + 1e-9:
            raise ValueError(f"Часы напоминания: от {MIN_REMINDER_HOURS} до {MAX_REMINDER_HOURS}.")
        seen.add(mi)
        cleaned.append(mi)
    if len(cleaned) > MAX_REMINDERS_PER_USER:
        raise ValueError(f"Не больше {MAX_REMINDERS_PER_USER} напоминаний.")
    db.execute(delete(UserReminderSetting).where(UserReminderSetting.user_id == int(user.id)))
    for i, mi in enumerate(cleaned):
        db.add(UserReminderSetting(user_id=int(user.id), minutes_before=mi, position=i))
    user.reminders_configured = True
    db.flush()


def add_user_reminder_hours(db: Session, user: User, hours_raw: Any) -> None:
    hours = parse_reminder_hours(hours_raw)
    minutes = hours_to_minutes(hours)
    current = get_effective_reminder_minutes(db, user)
    if minutes in current:
        raise ValueError("Такое напоминание уже есть.")
    if len(current) >= MAX_REMINDERS_PER_USER:
        raise ValueError(f"Не больше {MAX_REMINDERS_PER_USER} напоминаний.")
    new_list = list(current) + [minutes]
    new_list.sort(reverse=True)
    replace_user_reminder_minutes(db, user, new_list)


def remove_user_reminder_minutes(db: Session, user: User, minutes_before: int) -> None:
    current = get_effective_reminder_minutes(db, user)
    mi = int(minutes_before)
    if mi not in current:
        raise ValueError("Напоминание не найдено.")
    new_list = [x for x in current if x != mi]
    replace_user_reminder_minutes(db, user, new_list)


def clear_user_reminders(db: Session, user: User) -> None:
    replace_user_reminder_minutes(db, user, [])


def _booking_time_version(booking: Booking) -> str:
    pd = booking.planned_date
    if pd is None:
        return "none"
    return pd.replace(microsecond=0).isoformat(timespec="seconds")


def build_booking_reminder_message(
    booking: Booking,
    *,
    minutes_before: int,
    tz_name: str = DEFAULT_DISPLAY_TIMEZONE,
) -> str:
    when = format_naive_utc_datetime(booking.planned_date, tz_name, "%d.%m.%Y %H:%M") or "—"
    client_name = "—"
    if booking.client is not None:
        client_name = (booking.client.name or "").strip() or "—"
    service = _service_label(booking)
    comment = (booking.comment or "").strip()
    hours_label = minutes_to_hours_label(int(minutes_before))
    lines = [
        f"Напоминание: запись через {hours_label} ч",
        f"Дата/время: {when}",
        f"Клиент: {client_name}",
        f"Тип: {_booking_kind_label(booking.kind)}",
        f"Услуга: {service}",
    ]
    if comment:
        lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)


def _reminder_dedupe_key(
    *,
    booking_id: int,
    user_id: int,
    channel: NotificationChannel,
    time_version: str,
    minutes_before: int,
) -> str:
    return f"br:{booking_id}:{user_id}:{channel.value}:{time_version}:{int(minutes_before)}"


def _user_channel_targets(user: User) -> list[tuple[NotificationChannel, int]]:
    targets: list[tuple[NotificationChannel, int]] = []
    if user.vk_user_id is not None:
        targets.append((NotificationChannel.VK, int(user.vk_user_id)))
    if user.max_user_id is not None:
        targets.append((NotificationChannel.MAX, int(user.max_user_id)))
    if user.telegram_chat_id is not None:
        targets.append((NotificationChannel.TELEGRAM, int(user.telegram_chat_id)))
    return targets


def enqueue_booking_reminder(
    db: Session,
    booking: Booking,
    user: User,
    *,
    minutes_before: int,
    now: datetime | None = None,
) -> list[NotificationOutbox]:
    """Поставить напоминание во все подключённые каналы (с dedupe)."""
    from app.notify_prefs import user_wants_booking_notifications

    _ = now  # reserved for callers; lateness checked upstream
    if not user_wants_booking_notifications(user):
        return []
    targets = _user_channel_targets(user)
    if not targets:
        return []
    booking = _ensure_booking_loaded(db, booking)
    tz_name = get_display_timezone(db)
    text = build_booking_reminder_message(booking, minutes_before=minutes_before, tz_name=tz_name)
    time_version = _booking_time_version(booking)
    created: list[NotificationOutbox] = []
    for channel, target_id in targets:
        key = _reminder_dedupe_key(
            booking_id=int(booking.id),
            user_id=int(user.id),
            channel=channel,
            time_version=time_version,
            minutes_before=int(minutes_before),
        )
        exists = db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1))
        if exists is not None:
            continue
        payload = {
            "text": text,
            "target_id": target_id,
            "event_type": EVENT_BOOKING_REMINDER,
            "booking_id": int(booking.id),
            "minutes_before": int(minutes_before),
        }
        row = NotificationOutbox(
            user_id=int(user.id),
            booking_id=int(booking.id),
            channel=channel,
            event_type=EVENT_BOOKING_REMINDER,
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


def reminder_is_due(
    *,
    planned_date: datetime,
    minutes_before: int,
    now: datetime,
    lateness_window: timedelta = REMINDER_LATENESS_WINDOW,
) -> bool:
    """Момент (planned − minutes) наступил, запись ещё впереди, не старше окна давности."""
    if planned_date is None:
        return False
    if planned_date <= now:
        return False
    fire_at = planned_date - timedelta(minutes=int(minutes_before))
    if fire_at > now:
        return False
    if now - fire_at > lateness_window:
        return False
    return True


def enqueue_due_booking_reminders(
    db: Session,
    *,
    now: datetime | None = None,
    limit_bookings: int = 200,
) -> dict[str, int]:
    """Найти due-напоминания и поставить в outbox. Вызывается из воркера."""
    from app.notify_prefs import user_wants_booking_notifications

    now = now or utcnow_naive()
    max_horizon = timedelta(hours=MAX_REMINDER_HOURS)
    # planned в будущем, но не дальше макс. горизонта (+ небольшой запас под окно).
    upper = now + max_horizon + REMINDER_LATENESS_WINDOW
    bookings = list(
        db.scalars(
            select(Booking)
            .where(
                Booking.status.in_(tuple(OPEN_BOOKING_STATUSES)),
                Booking.planned_date > now,
                Booking.planned_date <= upper,
            )
            .options(
                selectinload(Booking.client),
                selectinload(Booking.planned_service),
                selectinload(Booking.masters),
                selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
                selectinload(Booking.planned_services).selectinload(BookingPlannedService.masters),
            )
            .order_by(Booking.planned_date.asc())
            .limit(max(1, int(limit_bookings)))
        ).all()
    )
    stats = {"checked": 0, "enqueued": 0, "users": 0}
    user_cache: dict[int, User] = {}
    reminder_cache: dict[int, list[int]] = {}

    for booking in bookings:
        stats["checked"] += 1
        master_ids = booking_planned_master_user_ids(booking)
        if not master_ids:
            continue
        for uid in master_ids:
            if uid not in user_cache:
                u = db.get(User, uid)
                if u is None:
                    continue
                user_cache[uid] = u
                reminder_cache[uid] = get_effective_reminder_minutes(db, u)
            user = user_cache[uid]
            if not user_wants_booking_notifications(user):
                continue
            if not _user_channel_targets(user):
                continue
            minutes_list = reminder_cache[uid]
            if not minutes_list:
                continue
            for minutes_before in minutes_list:
                if not reminder_is_due(
                    planned_date=booking.planned_date,
                    minutes_before=minutes_before,
                    now=now,
                ):
                    continue
                rows = enqueue_booking_reminder(
                    db,
                    booking,
                    user,
                    minutes_before=minutes_before,
                    now=now,
                )
                if rows:
                    stats["enqueued"] += len(rows)
                    stats["users"] += 1
    if stats["enqueued"]:
        db.flush()
    return stats
