"""Безопасная постановка/отправка уведомлений мастерам после сохранения брони."""

from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy.orm import Session

from app.consultation_booking import OPEN_BOOKING_STATUSES
from app.db.models import Booking, BookingStatus, NotificationChannel, NotificationOutbox
from app.display_time import get_display_timezone
from app.notifications import (
    EVENT_BOOKING_CANCELLED,
    EVENT_BOOKING_CREATED,
    EVENT_BOOKING_UPDATED,
    booking_content_version,
    booking_planned_master_user_ids,
    build_booking_master_message,
    enqueue_master_booking_notifications,
    process_outbox,
)
from app.time_utils import utcnow_naive

logger = logging.getLogger(__name__)

_TG_ONLY = (NotificationChannel.TELEGRAM,)


def booking_is_notifiable_for_masters(booking: Booking, *, for_cancel: bool = False) -> bool:
    """Не слать по прошлым броням и по статусам, где мастеру не нужна операционка."""
    if booking.planned_date is not None and booking.planned_date < utcnow_naive():
        return False
    if for_cancel:
        # Отмена/снятие — даже если статус уже CANCELLED.
        return True
    return booking.status in OPEN_BOOKING_STATUSES


def _flush_outbox(db: Session, rows: Sequence[NotificationOutbox]) -> None:
    if not rows:
        return
    ids = [int(r.id) for r in rows if r.id is not None]
    if not ids:
        return
    process_outbox(db, limit=max(len(ids), 1), only_ids=ids)


def _safe_enqueue_and_send(
    db: Session,
    booking: Booking,
    event_type: str,
    *,
    master_user_ids: Sequence[int] | None = None,
    text_override: str | None = None,
    version_override: str | None = None,
    unassigned: bool = False,
    for_cancel: bool = False,
) -> None:
    try:
        if not booking_is_notifiable_for_masters(booking, for_cancel=for_cancel):
            return
        rows = enqueue_master_booking_notifications(
            db,
            booking,
            event_type,
            master_user_ids=master_user_ids,
            text_override=text_override,
            version_override=version_override,
            channels=_TG_ONLY,
            unassigned=unassigned,
        )
        db.commit()
        try:
            _flush_outbox(db, rows)
            db.commit()
        except Exception:
            logger.exception(
                "booking notify: process_outbox failed booking_id=%s event=%s",
                getattr(booking, "id", None),
                event_type,
            )
            try:
                db.rollback()
            except Exception:
                pass
    except Exception:
        logger.exception(
            "booking notify: enqueue failed booking_id=%s event=%s",
            getattr(booking, "id", None),
            event_type,
        )
        try:
            db.rollback()
        except Exception:
            pass


def notify_booking_created(db: Session, booking_id: int) -> None:
    booking = db.get(Booking, int(booking_id))
    if booking is None:
        return
    _safe_enqueue_and_send(db, booking, EVENT_BOOKING_CREATED)


def notify_booking_cancelled(db: Session, booking_id: int) -> None:
    booking = db.get(Booking, int(booking_id))
    if booking is None:
        return
    _safe_enqueue_and_send(db, booking, EVENT_BOOKING_CANCELLED, for_cancel=True)


def notify_booking_updated_with_master_diff(
    db: Session,
    booking_id: int,
    *,
    old_master_ids: Sequence[int],
) -> None:
    """После правки брони: новым — created, снятым — cancelled (снята), остальным — updated (с dedupe)."""
    booking = db.get(Booking, int(booking_id))
    if booking is None:
        return

    # Если бронь отменили через статус в форме — всем текущим (и старым) шлём отмену.
    if booking.status == BookingStatus.CANCELLED:
        all_ids = sorted(set(int(x) for x in old_master_ids) | set(booking_planned_master_user_ids(booking)))
        _safe_enqueue_and_send(
            db,
            booking,
            EVENT_BOOKING_CANCELLED,
            master_user_ids=all_ids,
            for_cancel=True,
        )
        return

    if not booking_is_notifiable_for_masters(booking):
        return

    new_ids = set(booking_planned_master_user_ids(booking))
    old_ids = {int(x) for x in old_master_ids}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    retained = sorted(new_ids & old_ids)

    stamp = (booking.updated_at or utcnow_naive()).isoformat(timespec="seconds")
    content = booking_content_version(booking)

    if added:
        _safe_enqueue_and_send(
            db,
            booking,
            EVENT_BOOKING_CREATED,
            master_user_ids=added,
            version_override=f"assign:{stamp}:{content}",
        )
    if removed:
        tz_name = get_display_timezone(db)
        text = build_booking_master_message(
            booking, EVENT_BOOKING_CANCELLED, tz_name=tz_name, unassigned=True
        )
        _safe_enqueue_and_send(
            db,
            booking,
            EVENT_BOOKING_CANCELLED,
            master_user_ids=removed,
            text_override=text,
            version_override=f"unassign:{stamp}:{content}",
            unassigned=True,
            for_cancel=True,
        )
    if retained:
        _safe_enqueue_and_send(
            db,
            booking,
            EVENT_BOOKING_UPDATED,
            master_user_ids=retained,
        )
