"""Статусы брони: ждёт подтверждения / подтверждена."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.consultation_booking import booking_is_open, booking_status_label
from app.db.models import Booking, BookingKind, BookingStatus, Client, User, UserRole


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_booking_status_labels() -> None:
    assert booking_status_label(BookingStatus.PENDING_CONFIRMATION) == "😴 ждёт подтверждения"
    assert booking_status_label(BookingStatus.ACTIVE) == "✔️ подтверждена"
    assert booking_status_label(BookingStatus.DONE) == "✅ выполнена"
    assert booking_status_label(BookingStatus.CANCELLED) == "❌ отменена"
    assert booking_is_open(BookingStatus.PENDING_CONFIRMATION)
    assert booking_is_open(BookingStatus.ACTIVE)
    assert not booking_is_open(BookingStatus.DONE)


def test_new_booking_pending_and_confirm(memory_db) -> None:
    db = memory_db
    u = User(username="a1", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    db.add(u)
    c = Client(name="Клиент", is_confirmed=True)
    db.add(c)
    db.flush()
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 6, 1, 12, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
    )
    db.add(b)
    db.commit()
    row = db.scalar(select(Booking).where(Booking.id == b.id))
    assert row is not None
    assert row.status == BookingStatus.PENDING_CONFIRMATION
    row.status = BookingStatus.ACTIVE
    db.commit()
    db.refresh(row)
    assert row.status == BookingStatus.ACTIVE
