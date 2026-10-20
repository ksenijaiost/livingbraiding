"""3.38: контактное лицо клиента."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER, enqueue_client_rule_notifications
from app.client_contact import (
    PENDING_KIND_SELECTED,
    expand_phone_search_clients,
    validate_and_apply_contact,
    via_contact_preview,
)
from app.client_notifications import ensure_default_rules, list_rules, render_client_notify_template
from app.client_phone import DuplicateClientPhone
from app.db.base import Base
from app.db import models as _orm_models  # noqa: F401
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    ClientMessengerPending,
    ClientNotificationRuleKind,
    ClientNotificationSend,
    User,
    UserRole,
    VisitMastersScope,
)
from app.messenger_inbound import handle_client_text, suitable_bookings_for_reply_circle
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _user(db) -> User:
    u = User(
        username="adm_contact",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _client(db, *, name: str, phone: str | None = None, **kw) -> Client:
    c = Client(name=name, is_confirmed=True, **kw)
    if phone:
        from app.client_phone import apply_client_phone

        apply_client_phone(c, phone, db)
    db.add(c)
    db.flush()
    return c


def _booking(db, *, client_id: int, user_id: int, planned, status=BookingStatus.ACTIVE) -> Booking:
    b = Booking(
        client_id=client_id,
        kind=BookingKind.VISIT,
        status=status,
        planned_date=planned,
        masters_scope=VisitMastersScope.VISIT,
        details_json="{}",
        created_by_user_id=user_id,
    )
    db.add(b)
    db.flush()
    return b


def test_validate_no_chains_and_phone_rules(memory_db):
    db = memory_db
    mom = _client(db, name="Мама", phone="+79001112233")
    child = _client(db, name="Дочка")
    validate_and_apply_contact(
        db,
        child,
        contact_client_id_raw=str(mom.id),
        contact_relation_raw="мама",
        phone_normalized=None,
        is_create=True,
    )
    assert child.contact_client_id == mom.id
    assert child.contact_relation == "мама"

    # Контакт с номером, но сам со ссылкой на другое лицо (цепочка).
    proxy = _client(db, name="Прокси", phone="+79004445566")
    proxy.contact_client_id = mom.id
    db.flush()
    with pytest.raises(ValueError, match="уже указано"):
        other = _client(db, name="Внучка")
        validate_and_apply_contact(
            db,
            other,
            contact_client_id_raw=str(proxy.id),
            contact_relation_raw="мама",
            phone_normalized=None,
            is_create=True,
        )

    with_phone = _client(db, name="С телефоном", phone="+79005556677")
    warn = validate_and_apply_contact(
        db,
        with_phone,
        contact_client_id_raw=str(mom.id),
        contact_relation_raw="мама",
        phone_normalized=with_phone.phone_normalized,
        is_create=True,
    )
    assert warn is None
    assert with_phone.contact_client_id is None

    no_phone_contact = _client(db, name="Без номера контакта")
    child2 = _client(db, name="Ребёнок2")
    with pytest.raises(ValueError, match="номер"):
        validate_and_apply_contact(
            db,
            child2,
            contact_client_id_raw=str(no_phone_contact.id),
            contact_relation_raw="мама",
            phone_normalized=None,
            is_create=True,
        )


def test_validate_self_ref(memory_db):
    db = memory_db
    c = _client(db, name="Сам", phone="+79001234567")
    with pytest.raises(ValueError, match="сам себе"):
        validate_and_apply_contact(
            db,
            c,
            contact_client_id_raw=str(c.id),
            contact_relation_raw="я",
            phone_normalized=None,
            is_create=True,
        )
        # phone empty needed for contact — clear phone first
    c2 = _client(db, name="Сам2")
    c2.id  # flush already
    with pytest.raises(ValueError, match="сам себе"):
        validate_and_apply_contact(
            db,
            c2,
            contact_client_id_raw=str(c2.id),
            contact_relation_raw="я",
            phone_normalized=None,
            is_create=True,
        )


def test_notify_via_contact_prepends_line(memory_db):
    db = memory_db
    user = _user(db)
    mom = _client(db, name="Мама", phone="+79001112233", vk_user_id=111)
    child = _client(db, name="Дочка")
    child.contact_client_id = mom.id
    child.contact_relation = "мама"
    db.flush()
    planned = utcnow_naive() + timedelta(days=2)
    b = _booking(db, client_id=child.id, user_id=user.id, planned=planned)
    ensure_default_rules(db)
    rules = list_rules(db, ClientNotificationRuleKind.BEFORE_BOOKING)
    assert rules
    rows = enqueue_client_rule_notifications(db, b, child, rules[0])
    assert rows
    body = rows[0].body or ""
    assert "Запись для: Дочка" in body
    payload = __import__("json").loads(rows[0].payload_json)
    assert payload.get("via_contact_name") == "Мама"
    assert int(payload["target_id"]) == 111

    text = render_client_notify_template(
        "Привет, {{name}}",
        booking=b,
        client=child,
        tz_name="Asia/Novosibirsk",
        db=db,
        via_contact=mom,
    )
    assert text.startswith("Запись для: Дочка\n")
    text2 = render_client_notify_template(
        "{{client_for_line}}\nПривет",
        booking=b,
        client=child,
        tz_name="Asia/Novosibirsk",
        db=db,
        via_contact=mom,
    )
    assert text2.count("Запись для: Дочка") == 1


def test_inbound_contact_finds_dependent_booking(memory_db):
    db = memory_db
    user = _user(db)
    mom = _client(db, name="Мама", phone="+79001112233", telegram_chat_id=9001)
    child = _client(db, name="Дочка")
    child.contact_client_id = mom.id
    db.flush()
    planned = utcnow_naive() + timedelta(days=3)
    b = _booking(db, client_id=child.id, user_id=user.id, planned=planned)
    db.add(
        ClientNotificationSend(
            client_id=child.id,
            booking_id=b.id,
            channel="telegram",
            event_type=EVENT_CLIENT_BOOKING_REMINDER,
            target_id=9001,
            sent_at=utcnow_naive(),
        )
    )
    db.flush()
    answer = handle_client_text(db, channel="telegram", messenger_id=9001, text="1")
    assert answer
    db.refresh(b)
    assert b.client_confirmed_at is not None


def test_inbound_multi_client_choice(memory_db):
    db = memory_db
    user = _user(db)
    mom = _client(db, name="Мама", phone="+79001112233", telegram_chat_id=9002)
    child = _client(db, name="Дочка")
    child.contact_client_id = mom.id
    db.flush()
    planned1 = utcnow_naive() + timedelta(days=2)
    planned2 = utcnow_naive() + timedelta(days=5)
    b_mom = _booking(db, client_id=mom.id, user_id=user.id, planned=planned1)
    b_child = _booking(
        db,
        client_id=child.id,
        user_id=user.id,
        planned=planned2,
        status=BookingStatus.PENDING_CONFIRMATION,
    )
    now = utcnow_naive()
    db.add_all(
        [
            ClientNotificationSend(
                client_id=mom.id,
                booking_id=b_mom.id,
                channel="telegram",
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=9002,
                sent_at=now,
            ),
            ClientNotificationSend(
                client_id=child.id,
                booking_id=b_child.id,
                channel="telegram",
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=9002,
                sent_at=now - timedelta(minutes=1),
            ),
        ]
    )
    db.flush()
    suitable = suitable_bookings_for_reply_circle(db, mom)
    assert len({int(x.client_id) for x in suitable}) > 1

    answer = handle_client_text(db, channel="telegram", messenger_id=9002, text="1")
    assert answer and "Уточните" in answer
    pending = db.scalar(select(ClientMessengerPending).limit(1))
    assert pending is not None
    assert pending.kind == "await_booking_choice"

    # pick 2nd (Дочка) — order is by latest send first (mom), then child
    answer2 = handle_client_text(db, channel="telegram", messenger_id=9002, text="2")
    assert answer2 and "повторите" in answer2.lower()
    pending2 = db.scalar(select(ClientMessengerPending).limit(1))
    assert pending2 is not None
    assert pending2.kind == PENDING_KIND_SELECTED
    assert int(pending2.booking_id) == int(b_child.id)

    answer3 = handle_client_text(db, channel="telegram", messenger_id=9002, text="1")
    assert answer3
    db.refresh(b_child)
    assert b_child.client_confirmed_at is not None
    db.refresh(b_mom)
    assert b_mom.client_confirmed_at is None


def test_phone_search_finds_via(memory_db):
    db = memory_db
    mom = _client(db, name="Мама", phone="+79001112233")
    child = _client(db, name="Дочка")
    child.contact_client_id = mom.id
    child.contact_relation = "мама"
    db.flush()
    owners = list(
        db.scalars(select(Client).where(Client.phone_normalized == mom.phone_normalized)).all()
    )
    expanded = expand_phone_search_clients(db, owners)
    ids = {int(c.id) for c in expanded}
    assert int(mom.id) in ids
    assert int(child.id) in ids
    assert "через" in via_contact_preview(child, mom)


def test_duplicate_phone_html_has_client_link(memory_db):
    db = memory_db
    existing = _client(db, name="Уже есть", phone="+79009998877")
    exc = DuplicateClientPhone(existing)
    html = str(exc.html())
    assert f"/clients/{existing.id}" in html
    assert "Уже есть" in html
    # кнопка строится в форме из duplicate_contact_id
    assert exc.client_id == int(existing.id)
