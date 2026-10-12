"""3.13: карточка сотрудника — уведомления только инфо, без admin connect/notify_enabled."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import User, UserRole, UserRoleAssignment
from app.db.session import get_db
from app.main import app
from app.security import hash_password
from app.settings import get_settings


@pytest.fixture()
def memory_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _auth(user: User) -> AuthUser:
    return AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name,
        role=UserRole.ADMIN_SUPER,
        roles=(UserRole.ADMIN_SUPER,),
        master_level=user.master_level,
    )


def _seed_user(
    db,
    *,
    username: str,
    role: UserRole = UserRole.MASTER,
    notify_enabled: bool = True,
    telegram_chat_id: int | None = None,
    vk_user_id: int | None = None,
    reminders_configured: bool = False,
) -> User:
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username=username,
        password_hash=hash_password("secret1"),
        display_name=f"User {username}",
        role=role,
        is_active=True,
        notify_enabled=notify_enabled,
        telegram_chat_id=telegram_chat_id,
        vk_user_id=vk_user_id,
        reminders_configured=reminders_configured,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=role))
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [role], enabled=notify_enabled)
    db.commit()
    db.refresh(u)
    return u


def _client_for(db, admin: User) -> TestClient:
    auth = _auth(admin)

    def _override_user():
        return auth

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_staff_edit_page_opens_notify_info_only(memory_db) -> None:
    admin = _seed_user(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    master = _seed_user(
        memory_db,
        username="masha",
        notify_enabled=True,
        telegram_chat_id=1001,
        vk_user_id=None,
    )
    client = _client_for(memory_db, admin)
    try:
        r = client.get(f"/admin/settings/staff/{master.id}/edit", follow_redirects=False)
        assert r.status_code == 200
        assert "Уведомления" in r.text
        assert "Типы уведомлений:" in r.text
        assert "Telegram:</strong>" in r.text or "<strong>Telegram:</strong>" in r.text
        assert "Max:</strong>" in r.text or "<strong>Max:</strong>" in r.text
        assert "подключён" in r.text
        assert "не подключён" in r.text
        assert "по умолчанию 24ч+2ч" in r.text
        assert "Моя карточка" in r.text
        assert 'name="notify_enabled"' not in r.text
        assert "Подключить VK" not in r.text
        assert "Подключить Max" not in r.text
        assert "Подключить Telegram" not in r.text
        assert "Отключить VK" not in r.text
        assert "Отключить Max" not in r.text
        assert "Отключить Telegram" not in r.text
        assert f"/admin/settings/staff/{master.id}/vk/connect" not in r.text
        assert f"/admin/settings/staff/{master.id}/max/connect" not in r.text
        assert f"/admin/settings/staff/{master.id}/telegram/connect" not in r.text
    finally:
        _clear_overrides()


def test_staff_edit_post_does_not_change_notify_enabled(memory_db) -> None:
    admin = _seed_user(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    master = _seed_user(memory_db, username="masha", notify_enabled=True)
    client = _client_for(memory_db, admin)
    try:
        r = client.post(
            f"/admin/settings/staff/{master.id}/edit",
            data={
                "display_name": "Маша",
                "role_master": "1",
                "master_level": "MIDDLE",
                "is_active": "1",
                # notify_enabled намеренно не передаём
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        memory_db.refresh(master)
        assert master.notify_enabled is True
        assert master.display_name == "Маша"
    finally:
        _clear_overrides()


def test_staff_edit_reminders_cleared_summary(memory_db) -> None:
    admin = _seed_user(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    master = _seed_user(
        memory_db,
        username="masha",
        reminders_configured=True,
    )
    client = _client_for(memory_db, admin)
    try:
        r = client.get(f"/admin/settings/staff/{master.id}/edit", follow_redirects=False)
        assert r.status_code == 200
        assert "очищены" in r.text
    finally:
        _clear_overrides()
