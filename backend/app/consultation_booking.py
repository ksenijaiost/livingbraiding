"""Связь консультации и брони: статусы, доступность создания брони."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Booking, BookingStatus, Consultation


def booking_for_consultation(db: Session, consultation_id: int) -> Booking | None:
    return db.scalar(select(Booking).where(Booking.consultation_id == consultation_id).limit(1))


def booking_status_label(status: BookingStatus | None) -> str:
    if status is None:
        return "отсутствует"
    if status == BookingStatus.ACTIVE:
        return "активна"
    if status == BookingStatus.DONE:
        return "выполнена"
    if status == BookingStatus.CANCELLED:
        return "отменена"
    return str(status.value)


def can_create_booking_from_consultation(db: Session, c: Consultation) -> bool:
    b = booking_for_consultation(db, c.id)
    if b is None:
        return True
    return b.status == BookingStatus.CANCELLED


def consultation_has_open_booking(db: Session, consultation_id: int) -> bool:
    b = booking_for_consultation(db, consultation_id)
    return b is not None and b.status in (BookingStatus.ACTIVE, BookingStatus.DONE)
