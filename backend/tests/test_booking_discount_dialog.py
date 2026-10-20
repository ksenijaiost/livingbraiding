"""3.36: скидка % и «где диалог» у брони."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.booking_discount_dialog import (
    BOOKING_DISCOUNT_HINT_KEY,
    apply_booking_discount_to_visit_prefill,
    booking_discount_display,
    dialog_href,
    dialog_looks_like_link,
    parse_booking_dialog_location,
    parse_booking_discount_percent,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Booking, BookingKind, BookingStatus, Client, User, UserRole, VisitMastersScope
from app.routes.bookings import _parse_booking_discount_dialog_fields


def test_parse_discount_required_and_range() -> None:
    assert parse_booking_discount_percent("", required=True)[1]
    assert parse_booking_discount_percent("   ", required=True)[1]
    assert parse_booking_discount_percent("", required=False) == (None, None)
    assert parse_booking_discount_percent("0", required=True) == (0, None)
    assert parse_booking_discount_percent("100", required=True) == (100, None)
    assert parse_booking_discount_percent("101", required=True)[1]
    assert parse_booking_discount_percent("-1", required=True)[1]
    assert parse_booking_discount_percent("10.5", required=True)[1]
    assert parse_booking_discount_percent("abc", required=True)[1]


def test_parse_dialog_required_and_strip() -> None:
    assert parse_booking_dialog_location("", required=True)[1]
    assert parse_booking_dialog_location("   ", required=True)[1]
    assert parse_booking_dialog_location("", required=False) == (None, None)
    val, err = parse_booking_dialog_location("  https://t.me/x  ", required=True)
    assert err is None
    assert val == "https://t.me/x"


def test_display_helpers() -> None:
    assert booking_discount_display(None) == "не указано"
    assert booking_discount_display(0) == "без скидки"
    assert booking_discount_display(15) == "15%"
    assert dialog_looks_like_link("https://vk.com/im")
    assert dialog_looks_like_link("t.me/username")
    assert not dialog_looks_like_link("VK личка")
    assert dialog_href("t.me/x").startswith("https://")
    assert dialog_href("https://example.com") == "https://example.com"


def test_edit_optional_when_null_on_legacy_booking() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(username="a", password_hash="x", display_name="A", role=UserRole.ADMIN, is_active=True)
    client = Client(name="К", is_confirmed=True)
    db.add_all([user, client])
    db.flush()
    b = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 12, 1, 8, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        discount_percent=None,
        dialog_location=None,
    )
    db.add(b)
    db.flush()
    disc, dialog, errs = _parse_booking_discount_dialog_fields(
        {"discount_percent": "", "dialog_location": ""},
        require_discount=b.discount_percent is not None,
        require_dialog=b.dialog_location is not None,
    )
    assert errs == {}
    assert disc is None
    assert dialog is None
    _disc2, _dialog2, errs2 = _parse_booking_discount_dialog_fields(
        {"discount_percent": "", "dialog_location": ""},
        require_discount=True,
        require_dialog=True,
    )
    assert "discount_percent" in errs2
    assert "dialog_location" in errs2
    db.close()


def test_apply_booking_discount_to_visit_prefill() -> None:
    class _B:
        def __init__(self, pct):
            self.discount_percent = pct

    fp15: dict = {"line_1_service_id": "9"}
    apply_booking_discount_to_visit_prefill(fp15, _B(15))
    assert fp15["client_discount_percent"] == "15"
    assert fp15[BOOKING_DISCOUNT_HINT_KEY] == "15"
    assert fp15["line_1_client_discount_percent"] == "15"

    fp0: dict = {}
    apply_booking_discount_to_visit_prefill(fp0, _B(0))
    assert fp0["client_discount_percent"] == "0"
    assert fp0[BOOKING_DISCOUNT_HINT_KEY] == "0"

    fp_null: dict = {}
    apply_booking_discount_to_visit_prefill(fp_null, _B(None))
    assert "client_discount_percent" not in fp_null
    assert BOOKING_DISCOUNT_HINT_KEY not in fp_null

    fp_keep: dict = {"client_discount_percent": "20", "line_1_service_id": "3"}
    apply_booking_discount_to_visit_prefill(fp_keep, _B(15))
    assert fp_keep["client_discount_percent"] == "20"
    assert fp_keep[BOOKING_DISCOUNT_HINT_KEY] == "15"
    assert fp_keep["line_1_client_discount_percent"] == "15"
