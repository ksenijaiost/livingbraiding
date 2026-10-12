"""3.16: числовые ID каналов у clients (схема/ORM, без привязки)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Client


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db, engine


def test_client_channel_id_columns_exist(memory_db) -> None:
    _, engine = memory_db
    cols = {c["name"] for c in inspect(engine).get_columns("clients")}
    assert "telegram_chat_id" in cols
    assert "vk_user_id" in cols
    assert "max_user_id" in cols
    # Строковые контакты на месте.
    assert "telegram" in cols
    assert "vk" in cols


def test_client_channel_ids_nullable_and_orm(memory_db) -> None:
    db, _ = memory_db
    c = Client(name="Анна", phone="+79990001111", telegram="@anna", vk="vk.com/anna", is_confirmed=True)
    db.add(c)
    db.commit()
    db.refresh(c)
    assert c.telegram_chat_id is None
    assert c.vk_user_id is None
    assert c.max_user_id is None
    assert c.telegram == "@anna"
    assert c.vk == "vk.com/anna"

    c.telegram_chat_id = 111
    c.vk_user_id = 222
    c.max_user_id = 333
    db.commit()
    db.refresh(c)
    assert c.telegram_chat_id == 111
    assert c.vk_user_id == 222
    assert c.max_user_id == 333
    assert c.telegram == "@anna"


def test_client_channel_ids_unique(memory_db) -> None:
    db, _ = memory_db
    a = Client(name="A", is_confirmed=True, max_user_id=9001)
    b = Client(name="B", is_confirmed=True, max_user_id=9001)
    db.add(a)
    db.commit()
    db.add(b)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Несколько NULL допустимы.
    db.add_all(
        [
            Client(name="N1", is_confirmed=True),
            Client(name="N2", is_confirmed=True),
        ]
    )
    db.commit()
