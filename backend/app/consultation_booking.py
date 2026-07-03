"""Связь консультации и брони: статусы, доступность создания брони."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Booking, BookingKind, BookingStatus, Consultation


def booking_for_consultation(db: Session, consultation_id: int) -> Booking | None:
    return db.scalar(select(Booking).where(Booking.consultation_id == consultation_id).limit(1))


def consultation_for_booking(db: Session, booking_id: int) -> Consultation | None:
    return db.scalar(select(Consultation).where(Consultation.source_booking_id == booking_id).limit(1))


def can_create_consultation_from_booking(db: Session, booking_id: int) -> bool:
    b = db.get(Booking, booking_id)
    if b is None or b.kind != BookingKind.CONSULTATION:
        return False
    if b.status == BookingStatus.CANCELLED:
        return False
    return consultation_for_booking(db, booking_id) is None


OPEN_BOOKING_STATUSES = frozenset(
    {BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE}
)


def booking_status_label(status: BookingStatus | None) -> str:
    if status is None:
        return "➖ отсутствует"
    if status == BookingStatus.PENDING_CONFIRMATION:
        return "😴 ждёт подтверждения"
    if status == BookingStatus.ACTIVE:
        return "✔️ подтверждена"
    if status == BookingStatus.DONE:
        return "✅ выполнена"
    if status == BookingStatus.CANCELLED:
        return "❌ отменена"
    return str(status.value)


def booking_status_display(status: BookingStatus | object | str | None) -> str:
    """Подпись статуса для шаблонов: enum брони, строка value или None."""
    if status is None:
        return booking_status_label(None)
    if isinstance(status, BookingStatus):
        return booking_status_label(status)
    val = getattr(status, "value", status)
    try:
        return booking_status_label(BookingStatus(val))
    except (ValueError, TypeError):
        return str(val)


def booking_kind_label(kind: BookingKind | object | str | None) -> str:
    """Краткая подпись типа брони для списков и шаблонов."""
    if kind is None:
        return "—"
    k = kind.value if isinstance(kind, BookingKind) else str(getattr(kind, "value", kind))
    if k == BookingKind.VISIT.value:
        return "Визит"
    if k == BookingKind.PRODUCT_SALE.value:
        return "Продажа"
    if k == BookingKind.CONSULTATION.value:
        return "Консультация"
    return k


def booking_is_open(status: BookingStatus) -> bool:
    """Бронь не отменена и не выполнена — можно работы/визиты/продажи и отмену."""
    return status in OPEN_BOOKING_STATUSES


def can_create_booking_from_consultation(db: Session, c: Consultation) -> bool:
    b = booking_for_consultation(db, c.id)
    if b is None:
        return True
    return b.status == BookingStatus.CANCELLED


def consultation_has_open_booking(db: Session, consultation_id: int) -> bool:
    b = booking_for_consultation(db, consultation_id)
    return b is not None and b.status in (
        BookingStatus.PENDING_CONFIRMATION,
        BookingStatus.ACTIVE,
        BookingStatus.DONE,
    )
