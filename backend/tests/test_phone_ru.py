"""3.32: российский телефон клиента — нормализация, поиск, миграция, дубли."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.client_phone import DuplicateClientPhone, apply_client_phone
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Client
from app.phone_ru import (
    InvalidPhoneError,
    backfill_client_phone_normalized,
    format_phone,
    normalize_phone,
    phone_search_digits,
)
from app.routes.clients import _client_suggest_items
from app.routes.master_clients import _client_suggest_items as _master_suggest


def test_normalize_and_format_examples() -> None:
    assert normalize_phone("+7 901 123 45 67") == "79011234567"
    assert normalize_phone("89011234567") == "79011234567"
    assert normalize_phone("8(901)123-45-67") == "79011234567"
    assert normalize_phone("9011234567") == "79011234567"
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None
    assert normalize_phone(None) is None
    assert format_phone("79011234567") == "+7 (901) 123-45-67"
    for raw in ("123456789", "790112345678", "абв", "12аб"):
        with pytest.raises(InvalidPhoneError, match="Некорректный номер телефона"):
            normalize_phone(raw)


def test_search_digits_examples() -> None:
    assert phone_search_digits("8901") == "7901"
    assert phone_search_digits("+7 901") == "7901"
    assert phone_search_digits("901 123") == "901123"
    assert phone_search_digits("1234567") == "1234567"
    assert phone_search_digits("89011234567") == "79011234567"
    assert phone_search_digits("123") is None
    assert phone_search_digits("Анна") is None


def test_suggest_finds_one_client_by_different_queries() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    client = Client(name="Лариса", is_confirmed=True)
    apply_client_phone(client, "8(901)123-45-67", db)
    db.add(client)
    db.commit()
    for query in ("8901", "+7 901", "901 123", "1234567"):
        found = _client_suggest_items(db, query)
        assert [item["id"] for item in found] == [client.id]
        master = _master_suggest(db, query)
        assert master[0]["id"] == client.id
        assert "+7 (901) 123-45-67" in str(master[0]["hint"])
    assert _client_suggest_items(db, "12") == []
    db.close()


def test_duplicate_phone_is_rejected() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    first = Client(name="Анна", is_confirmed=True)
    apply_client_phone(first, "9011234567", db)
    db.add(first)
    db.commit()
    second = Client(name="Мария", is_confirmed=True)
    with pytest.raises(DuplicateClientPhone, match="Анна") as caught:
        apply_client_phone(second, "+7 901 123-45-67", db)
    assert "/clients/" in str(caught.value)
    assert caught.value.html()
    db.close()


def test_backfill_keeps_unknown_and_rejects_duplicates() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE clients (
                    id INTEGER PRIMARY KEY,
                    phone VARCHAR(30),
                    phone_normalized VARCHAR(11)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO clients (id, phone) VALUES
                (1, '+7 901 123 45 67'),
                (2, 'абв'),
                (3, NULL),
                (4, '')
                """
            )
        )
        unknown = backfill_client_phone_normalized(conn)
        assert unknown == [(2, "абв")]
        assert conn.execute(text("SELECT phone_normalized FROM clients WHERE id = 1")).scalar() == "79011234567"
        assert conn.execute(text("SELECT phone_normalized FROM clients WHERE id = 2")).scalar() is None
        assert conn.execute(text("SELECT phone_normalized FROM clients WHERE id = 3")).scalar() is None

    engine2 = create_engine("sqlite://")
    with engine2.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE clients (
                    id INTEGER PRIMARY KEY,
                    phone VARCHAR(30),
                    phone_normalized VARCHAR(11)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO clients (id, phone) VALUES
                (10, '89011234567'),
                (11, '9011234567')
                """
            )
        )
        with pytest.raises(RuntimeError, match="10") as caught:
            backfill_client_phone_normalized(conn)
        assert "11" in str(caught.value)
        assert "79011234567" in str(caught.value)
