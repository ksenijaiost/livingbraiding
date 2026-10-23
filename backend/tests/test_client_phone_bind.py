"""3.49: короткий код привязки и подключение клиента по номеру телефона."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Client, ClientInboundMessage, TelegramLinkToken, User, UserRole
from app.messenger_inbound import process_inbound_message
from app.telegram_link import (
    MSG_ASK_CLIENT_PHONE,
    MSG_CLIENT_ALREADY_LINKED,
    MSG_CLIENT_LINKED_OK,
    MSG_CLIENT_MAX_TAKEN,
    MSG_CLIENT_PHONE_NOT_FOUND,
    _LINK_CODE_ALPHABET,
    consume_any_link_token,
    create_client_link_token,
    create_link_token,
    extract_phone_for_client_bind,
    hash_link_token,
    is_client_bind_greeting_or_command,
    parse_max_link_code,
    parse_vk_link_code,
)
from app.time_utils import utcnow_naive


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _client(db, *, phone: str = "79131234567", name: str = "Катя") -> Client:
    row = Client(name=name, phone="+7 (913) 123-45-67", phone_normalized=phone, is_confirmed=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _answers(db, channel: str, messenger_id: int, text: str) -> list[str]:
    sent: list[str] = []

    def _capture(_channel: str, _mid: int, body: str) -> None:
        sent.append(body)

    with patch("app.messenger_inbound.reply_to_messenger", side_effect=_capture):
        process_inbound_message(db, channel=channel, messenger_id=messenger_id, text=text)
    return sent


def test_client_link_code_is_short_and_case_insensitive(db) -> None:
    client = _client(db)
    plain, _link = create_client_link_token(db, client.id, channel="vk")
    db.commit()
    assert len(plain) == 6
    assert plain == plain.lower()
    assert any(ch.isalpha() for ch in plain)
    assert set(plain) <= set(_LINK_CODE_ALPHABET)
    assert parse_vk_link_code(plain.upper()) == plain.upper()
    assert parse_vk_link_code("89131234567") is None
    assert parse_vk_link_code("привязка 89131234567") is None
    assert parse_max_link_code(text="привет") is None
    assert parse_max_link_code(text=f"привязка {plain}") == plain

    user, bound = consume_any_link_token(db, plain.upper(), channel="vk")
    assert user is None
    assert bound is not None and int(bound.id) == int(client.id)

    staff = User(
        username="master",
        password_hash="x",
        display_name="Маша",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(staff)
    db.commit()
    staff_code, _ = create_link_token(db, staff.id, channel="telegram")
    assert len(staff_code) == 6 and any(ch.isalpha() for ch in staff_code)

    old_plain = "AbC12x"
    now = utcnow_naive()
    db.add(
        TelegramLinkToken(
            user_id=None,
            client_id=client.id,
            token_hash=hash_link_token(old_plain),
            channel="max",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            used_at=None,
        )
    )
    db.commit()
    _user, old_client = consume_any_link_token(db, old_plain, channel="max")
    assert old_client is not None and int(old_client.id) == int(client.id)


def test_phone_extract_examples() -> None:
    assert extract_phone_for_client_bind("89131234567") == "79131234567"
    assert extract_phone_for_client_bind("+7 (913) 123-45-67") == "79131234567"
    assert extract_phone_for_client_bind("Привязка 89131234567") == "79131234567"
    assert extract_phone_for_client_bind("Привет +79131234567") == "79131234567"
    assert extract_phone_for_client_bind("Привет") is None
    assert is_client_bind_greeting_or_command("/start")
    assert is_client_bind_greeting_or_command("Привязка")
    assert is_client_bind_greeting_or_command("Здравствуйте!")


def test_bind_by_phone_one_step_all_channels(db) -> None:
    client = _client(db)
    samples = ("89131234567", "+7 (913) 123-45-67", "Привязка 89131234567", "Привет +79131234567")
    channels = (("vk", "vk_user_id"), ("max", "max_user_id"), ("telegram", "telegram_chat_id"))
    messenger_id = 5000
    for channel, field in channels:
        for text in samples:
            sent = _answers(db, channel, messenger_id, text)
            db.refresh(client)
            assert sent == [MSG_CLIENT_LINKED_OK]
            assert getattr(client, field) == messenger_id
            logged = db.scalar(
                select(ClientInboundMessage)
                .where(ClientInboundMessage.channel == channel)
                .order_by(ClientInboundMessage.id.desc())
                .limit(1)
            )
            assert logged is not None
            assert logged.parsed_action == "bind_phone"
            assert int(logged.client_id) == int(client.id)
            setattr(client, field, None)
            db.commit()
            messenger_id += 1


def test_bind_by_phone_two_steps_then_number(db) -> None:
    client = _client(db)
    for text in ("Привет", "/start", "Привязка"):
        sent = _answers(db, "vk", 42, text)
        assert sent == [MSG_ASK_CLIENT_PHONE]
        db.refresh(client)
        assert client.vk_user_id is None
    sent = _answers(db, "vk", 42, "89131234567")
    db.refresh(client)
    assert sent == [MSG_CLIENT_LINKED_OK]
    assert client.vk_user_id == 42


def test_phone_not_found_and_channel_taken(db) -> None:
    sent = _answers(db, "telegram", 77, "89001112233")
    assert sent == [MSG_CLIENT_PHONE_NOT_FOUND]
    missing = db.scalar(select(ClientInboundMessage).limit(1))
    assert missing is not None and missing.client_id is None and missing.parsed_action == "bind_phone"

    first = _client(db, phone="79001111111", name="Аня")
    second = _client(db, phone="79002222222", name="Оля")
    first.vk_user_id = 9
    db.commit()
    sent = _answers(db, "vk", 9, "89002222222")
    db.refresh(second)
    assert second.vk_user_id is None
    assert sent == ["Этот VK уже привязан к другому клиенту"]

    second.max_user_id = None
    other = _client(db, phone="79003333333", name="Ира")
    other.max_user_id = 15
    db.commit()
    sent = _answers(db, "max", 15, "89002222222")
    assert sent == [MSG_CLIENT_MAX_TAKEN]


def test_already_linked_client_and_staff_hint(db) -> None:
    client = _client(db)
    client.telegram_chat_id = 300
    staff = User(
        username="staff",
        password_hash="x",
        display_name="Лена",
        role=UserRole.MASTER,
        is_active=True,
        vk_user_id=800,
    )
    db.add(staff)
    db.commit()
    assert _answers(db, "telegram", 300, "Уведомления") == [MSG_CLIENT_ALREADY_LINKED]
    assert _answers(db, "vk", 800, "привязка") == [MSG_ASK_CLIENT_PHONE]
    assert _answers(db, "vk", 800, "89131234567") == [MSG_CLIENT_LINKED_OK]
    db.refresh(client)
    assert client.vk_user_id == 800
