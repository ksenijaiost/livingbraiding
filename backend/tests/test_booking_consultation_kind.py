"""Бронь типа CONSULTATION (Fix 97)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.consultation_booking import can_create_consultation_from_booking, consultation_for_booking
from app.consultation_types import types_json_dumps
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    Consultation,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.planned_services_db import booking_planned_service_ids


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed(db):
    admin = User(
        username="a",
        password_hash="x",
        display_name="Admin",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    client = Client(name="C", phone="+79990000002", is_confirmed=True)
    db.add(client)
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.commit()
    return int(admin.id), int(client.id)


def test_consultation_booking_and_link(memory_db) -> None:
    db = memory_db
    _admin_id, client_id = _seed(db)
    planned = datetime(2026, 4, 15, 10, 30, 0)
    b = Booking(
        created_by_user_id=_admin_id,
        client_id=client_id,
        planned_date=planned,
        kind=BookingKind.CONSULTATION,
        status=BookingStatus.ACTIVE,
        details_json=types_json_dumps({"BRAIDING": True}),
    )
    db.add(b)
    db.commit()
    db.refresh(b)

    assert can_create_consultation_from_booking(db, b.id)
    c = Consultation(
        created_by_user_id=_admin_id,
        client_id=client_id,
        source_booking_id=b.id,
        consultation_date=planned,
        types_json=types_json_dumps({"BRAIDING": True}),
    )
    db.add(c)
    db.commit()

    linked = consultation_for_booking(db, b.id)
    assert linked is not None
    assert int(linked.id) == int(c.id)
    assert not can_create_consultation_from_booking(db, b.id)
    assert booking_planned_service_ids(db, b.id) == []
