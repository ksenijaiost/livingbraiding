"""3.26: массовое добавление клиентов из JSON."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.client_bulk_import import (
    MAX_BULK_CLIENTS,
    MAX_BULK_JSON_BYTES,
    STATUS_CREATED,
    STATUS_DUPLICATE,
    STATUS_ERROR,
    import_single_client_row,
    parse_bulk_clients_json,
)
from app.client_status import ClientStatus, client_display_status
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Client


def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture()
def db():
    session = memory_db()
    try:
        yield session
    finally:
        session.close()


def test_parse_empty_not_array_and_limit() -> None:
    with pytest.raises(ValueError, match="Введите JSON"):
        parse_bulk_clients_json("   ")
    with pytest.raises(ValueError, match="массивом"):
        parse_bulk_clients_json('{"name": "Анна"}')
    with pytest.raises(ValueError, match="большой"):
        parse_bulk_clients_json("[" + (" " * (MAX_BULK_JSON_BYTES + 10)) + "]")
    too_many = json.dumps([{"name": "А"}] * (MAX_BULK_CLIENTS + 1))
    with pytest.raises(ValueError, match="много строк"):
        parse_bulk_clients_json(too_many)
    rows = parse_bulk_clients_json('[{"name": "Анна", "telegram": "@a"}]')
    assert rows[0]["name"] == "Анна"


def test_create_client_without_visit(db) -> None:
    reserved: set[str] = set()
    result = import_single_client_row(
        db,
        {
            "name": "  Анна  ",
            "phone": "  +7 999 000-11-22  ",
            "telegram": "@anna",
            "birth_day": 5,
            "birth_month": 3,
            "age_group": "18_30",
            "source": "Вконтакте",
            "comment": "из базы",
            "no_promo": True,
            "is_student": True,
        },
        reserved_phones=reserved,
        changed_by_user_id=7,
        line=1,
    )
    assert result["status"] == STATUS_CREATED
    assert result["ok"] is True
    assert result["phone"] == "+7 (999) 000-11-22"
    client = db.get(Client, result["client_id"])
    assert client is not None
    assert client.name == "Анна"
    assert client.created_by_label == "Массовое добавление"
    assert client.updated_by_user_id == 7
    assert client.birth_year is None
    assert client.birth_day == 5
    assert client.no_promo is True
    assert client.is_student is True
    assert client_display_status(db, client) == ClientStatus.NEVER
    assert "79990001122" in reserved

    # 3.38: без телефона нужен привязанный канал или контактное лицо — одной ссылки на соцсеть мало.
    no_phone = import_single_client_row(
        db,
        {"name": "Мария", "telegram": "@maria"},
        reserved_phones=reserved,
        changed_by_user_id=7,
        line=2,
    )
    assert no_phone["status"] == STATUS_ERROR
    assert "контактное лицо" in no_phone["message"].lower()
    with_phone = import_single_client_row(
        db,
        {"name": "Мария", "telegram": "@maria", "phone": "79990003344"},
        reserved_phones=reserved,
        changed_by_user_id=7,
        line=3,
    )
    assert with_phone["status"] == STATUS_CREATED
    again = import_single_client_row(
        db,
        {"name": "Мария", "vk": "vk.com/maria", "phone": "79990005566"},
        reserved_phones=reserved,
        changed_by_user_id=7,
        line=4,
    )
    assert again["status"] == STATUS_CREATED
    assert db.scalar(select(func.count()).select_from(Client)) == 3


def test_duplicate_phone_in_db_and_batch(db) -> None:
    from app.client_phone import apply_client_phone

    existing = Client(name="Уже есть", is_confirmed=True)
    apply_client_phone(existing, "79990001122", db)
    db.add(existing)
    db.commit()
    existing = db.scalar(select(Client).where(Client.phone_normalized == "79990001122"))
    reserved: set[str] = set()
    skipped = import_single_client_row(
        db,
        {"name": "Копия", "phone": "8 (999) 000-11-22"},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=1,
    )
    assert skipped["status"] == STATUS_DUPLICATE
    assert "Уже есть" in skipped["message"]
    assert "+7 (999) 000-11-22" in skipped["message"]
    assert skipped["client_id"] == existing.id
    assert db.scalar(select(func.count()).select_from(Client)) == 1

    reserved2: set[str] = set()
    first = import_single_client_row(
        db,
        {"name": "Первая", "phone": "70001112233"},
        reserved_phones=reserved2,
        changed_by_user_id=1,
        line=1,
    )
    second = import_single_client_row(
        db,
        {"name": "Вторая", "phone": "70001112233", "telegram": "@x"},
        reserved_phones=reserved2,
        changed_by_user_id=1,
        line=2,
    )
    assert first["status"] == STATUS_CREATED
    assert second["status"] == STATUS_DUPLICATE
    assert second["client_id"] == first["client_id"]
    assert db.scalar(select(func.count()).select_from(Client).where(Client.phone_normalized == "70001112233")) == 1


def test_unknown_key_and_bad_birth_do_not_block_next_row(db) -> None:
    reserved: set[str] = set()
    unknown = import_single_client_row(
        db,
        {"name": "Опечатка", "phone": "71112223344", "emali": "a@b.c"},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=1,
    )
    assert unknown["status"] == STATUS_ERROR
    assert "emali" in unknown["message"]
    bad_birth = import_single_client_row(
        db,
        {"name": "Дата", "telegram": "@d", "birth_day": 5},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=2,
    )
    assert bad_birth["status"] == STATUS_ERROR
    assert "месяц" in bad_birth["message"].lower() or "день" in bad_birth["message"].lower()
    bad_month = import_single_client_row(
        db,
        {"name": "Месяц", "telegram": "@m", "birth_day": 1, "birth_month": 13},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=3,
    )
    assert bad_month["status"] == STATUS_ERROR
    ok = import_single_client_row(
        db,
        {"name": "Нормальная", "telegram": "@ok", "phone": "79995554433"},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=4,
    )
    assert ok["status"] == STATUS_CREATED
    bad_age = import_single_client_row(
        db,
        {"name": "Возраст", "telegram": "@age", "age_group": "детский"},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=5,
    )
    bad_phone = import_single_client_row(
        db,
        {"name": "Кривой", "phone": "12345"},
        reserved_phones=reserved,
        changed_by_user_id=1,
        line=6,
    )
    assert bad_phone["status"] == STATUS_ERROR
    assert bad_phone["message"] == "Некорректный номер телефона"
    assert bad_age["status"] == STATUS_ERROR
    assert "18_30" in bad_age["message"]
    assert db.scalar(select(func.count()).select_from(Client)) == 1
