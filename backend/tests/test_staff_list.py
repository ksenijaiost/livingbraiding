"""3.9: компактный список сотрудников /admin/settings/staff."""

from __future__ import annotations

import re

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


def _seed_staff(
    db,
    *,
    username: str,
    display_name: str | None = None,
    role: UserRole = UserRole.MASTER,
    is_active: bool = True,
    telegram_chat_id: int | None = None,
    vk_user_id: int | None = None,
    notify_enabled: bool = True,
) -> User:
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username=username,
        password_hash=hash_password("secret1"),
        display_name=display_name or f"User {username}",
        role=role,
        is_active=is_active,
        phone=None,
        telegram_chat_id=telegram_chat_id,
        vk_user_id=vk_user_id,
        notify_enabled=notify_enabled,
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


def _row_html(page: str, username: str) -> str:
    """Фрагмент <tr>…</tr> для сотрудника по логину."""
    m = re.search(
        rf'<tr class="lb-staff-row[^"]*">[\s\S]*?<code>{re.escape(username)}</code>[\s\S]*?</tr>',
        page,
    )
    assert m, f"row for {username!r} not found"
    return m.group(0)


def test_staff_list_page_opens(memory_db) -> None:
    admin = _seed_staff(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    client = _client_for(memory_db, admin)
    try:
        r = client.get("/admin/settings/staff", follow_redirects=False)
        assert r.status_code == 200
        assert "Сотрудники" in r.text
        assert "lb-staff-table" in r.text
        assert "super1" in r.text
    finally:
        _clear_overrides()


def test_staff_list_active_inactive_icons(memory_db) -> None:
    admin = _seed_staff(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    _seed_staff(memory_db, username="active_m", is_active=True)
    _seed_staff(memory_db, username="off_m", is_active=False)
    client = _client_for(memory_db, admin)
    try:
        r = client.get("/admin/settings/staff", follow_redirects=False)
        assert r.status_code == 200
        active_row = _row_html(r.text, "active_m")
        off_row = _row_html(r.text, "off_m")
        assert 'title="Активен"' in active_row
        assert "✅" in active_row
        assert 'title="Не активен"' in off_row
        assert "❌" in off_row
    finally:
        _clear_overrides()


@pytest.mark.parametrize(
    "username,tg,vk,notify,expect_ok,title_bits",
    [
        ("n_none", None, None, True, False, ["каналы не подключены"]),
        ("n_vk", None, 111, True, True, ["VK"]),
        ("n_tg", 222, None, True, True, ["Telegram"]),
        ("n_off", 333, None, False, False, ["Telegram", "уведомления выключены переключателем"]),
    ],
)
def test_staff_list_notify_status(memory_db, username, tg, vk, notify, expect_ok, title_bits) -> None:
    admin = _seed_staff(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    _seed_staff(
        memory_db,
        username=username,
        telegram_chat_id=tg,
        vk_user_id=vk,
        notify_enabled=notify,
    )
    client = _client_for(memory_db, admin)
    try:
        r = client.get("/admin/settings/staff", follow_redirects=False)
        assert r.status_code == 200
        row = _row_html(r.text, username)
        if expect_ok:
            assert 'aria-label="Уведомления включены"' in row
            assert "✅" in row
        else:
            assert 'aria-label="Уведомления выключены или канал не подключён"' in row
            # Активен ✅ + уведомления ❌ — оба символа могут быть; проверяем title уведомлений.
        for bit in title_bits:
            assert bit in row
        if not expect_ok and notify is False:
            assert "уведомления выключены переключателем" in row
            assert "❌" in row
        if not expect_ok and tg is None and vk is None:
            assert "каналы не подключены" in row
            assert "❌" in row
    finally:
        _clear_overrides()


def test_staff_list_roles_stacked(memory_db) -> None:
    admin = _seed_staff(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    u = _seed_staff(memory_db, username="multi", role=UserRole.MASTER)
    memory_db.add(UserRoleAssignment(user_id=u.id, role=UserRole.ADMIN))
    memory_db.commit()
    client = _client_for(memory_db, admin)
    try:
        r = client.get("/admin/settings/staff", follow_redirects=False)
        assert r.status_code == 200
        row = _row_html(r.text, "multi")
        assert row.count('class="lb-staff-role"') >= 2
        assert "lb-role-chip" not in row
    finally:
        _clear_overrides()
