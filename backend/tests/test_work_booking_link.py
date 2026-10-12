"""Привязка работы к брони по номеру из поиска и снимок цены без граммов."""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.auth import AuthUser
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    CatalogProduct,
    Client,
    MasterLevel,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    VisitMastersScope,
    WorkForInventory,
)
from app.work_products import _booking_query_digits, _material_snap_float, work_new_post


class _FakeForm(dict):
    def get(self, key, default=None):
        return super().get(key, default)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _auth(master: User) -> AuthUser:
    return AuthUser(
        id=master.id,
        username=master.username,
        display_name=master.display_name,
        role=UserRole.MASTER,
        roles=(UserRole.MASTER,),
        master_level=MasterLevel.MIDDLE,
    )


def _seed(db) -> tuple[User, Client, Booking]:
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    user = User(
        username="irishka",
        password_hash="x",
        display_name="Ира",
        role=UserRole.MASTER,
        is_active=True,
    )
    client = Client(name="Лариса Никитченко", phone="79001112233", is_confirmed=True)
    db.add_all([user, client])
    db.flush()
    db.add(UserRoleAssignment(user_id=user.id, role=UserRole.MASTER))
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="DE Curl",
            price=100.0,
            meta_json='{"kit_key": "DE_CURL", "master_pay": 10}',
            is_active=True,
        )
    )
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 10, 9, 0, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.commit()
    db.refresh(user)
    db.refresh(client)
    db.refresh(booking)
    return user, client, booking


def _post(db, master: User, fields: dict[str, str]):
    body = urlencode(fields).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/sales/work/new",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "query_string": b"",
    }
    return asyncio.run(work_new_post(Request(scope, receive), _auth(master), db))


def _kit_fields(master: User, **extra: str) -> dict[str, str]:
    fields = {
        "performed_date": "2026-10-05",
        "scope": "CUSTOM_ORDER",
        "kind": "KIT",
        "kit_type_de": "on",
        "kit_line_0_key": "DE_CURL",
        f"kit_line_0_qty_{master.id}": "2",
        "kit_line_0_used_pct": "100",
        "kanekalon_grams": "0",
        "kudri_grams": "0",
        "order_booking_mode": "with_booking",
        "booking_id": "",
        "client_id": "",
    }
    fields.update(extra)
    return fields


def test_material_snap_float_none_is_zero() -> None:
    assert _material_snap_float(None) == 0.0
    assert _material_snap_float(12.5) == 12.5


def test_booking_query_digits() -> None:
    assert _booking_query_digits(_FakeForm({"booking_query": " #416 "})) == "416"
    assert _booking_query_digits(_FakeForm({"booking_query": "Лариса"})) == ""


def test_kit_order_without_grams_does_not_crash(memory_db):
    user, client, _booking = _seed(memory_db)
    resp = _post(
        memory_db,
        user,
        _kit_fields(
            user,
            order_booking_mode="without_booking",
            client_id=str(client.id),
        ),
    )
    assert resp.status_code == 303, getattr(resp, "body", resp)
    work = memory_db.scalars(select(WorkForInventory).order_by(WorkForInventory.id.desc())).first()
    assert work is not None
    assert work.client_id == client.id
    assert work.booking_id is None
    assert work.kanekalon_grams == 0


def test_booking_number_in_search_links_work_and_client(memory_db):
    user, client, booking = _seed(memory_db)
    resp = _post(
        memory_db,
        user,
        _kit_fields(user, booking_query=str(booking.id)),
    )
    assert resp.status_code == 303, getattr(resp, "body", resp)
    work = memory_db.scalars(select(WorkForInventory).order_by(WorkForInventory.id.desc())).first()
    assert work is not None
    assert work.booking_id == booking.id
    assert work.client_id == client.id


def test_unknown_booking_number_is_validation_error(memory_db):
    user, _client, _booking = _seed(memory_db)
    resp = _post(memory_db, user, _kit_fields(user, booking_query="999416"))
    assert resp.status_code == 400
    assert "999416" in str(resp.context.get("error") or "")


def test_with_booking_and_empty_search_keeps_choose_message(memory_db):
    user, _client, _booking = _seed(memory_db)
    resp = _post(memory_db, user, _kit_fields(user))
    assert resp.status_code == 400
    assert "Выберите бронь" in str(resp.context.get("error") or "")
