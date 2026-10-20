"""3.27: код переменной на странице уведомлений и отказ от booking_link."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.client_notifications import KIND_AFTER
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import ClientNotificationRule, User, UserRole
from app.db.session import get_db
from app.main import app
from app.studio_template_variables import add_custom_variable


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _client(db) -> TestClient:
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.commit()
    auth = AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name,
        role=UserRole.ADMIN,
        roles=(UserRole.ADMIN,),
    )

    def _user():
        return auth

    def _db():
        yield db

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_palette_shows_tokens_not_dict_copy(db) -> None:
    add_custom_variable(db, "parking", "за домом", user_id=None)
    db.commit()
    client = _client(db)
    try:
        res = client.get("/clients/notifications")
        assert res.status_code == 200
        html = res.text
        assert "{{address}}" in html
        assert "{{date}}" in html
        assert "{{parking}}" in html
        assert 'data-copy="{{address}}"' in html
        assert 'data-copy="{{date}}"' in html
        assert 'data-copy="{{parking}}"' in html
        assert "built-in method" not in html
        assert 'data-copy="{{booking_link}}"' not in html
        assert "Чтобы записаться снова, просто напишите нам здесь" in html
    finally:
        app.dependency_overrides.clear()


def test_warning_when_template_keeps_booking_link(db) -> None:
    client = _client(db)
    try:
        assert client.get("/clients/notifications").status_code == 200
        rule = db.scalar(select(ClientNotificationRule).where(ClientNotificationRule.kind == KIND_AFTER))
        assert rule is not None
        rule.template = "Спасибо, {{name}}.\nЗаписаться снова: {{booking_link}}"
        db.commit()
        html = client.get("/clients/notifications").text
        assert "Этой переменной больше нет" in html
        assert "{{booking_link}}" in html
        db.refresh(rule)
        assert rule.template == "Спасибо, {{name}}.\nЗаписаться снова: {{booking_link}}"
    finally:
        app.dependency_overrides.clear()
