"""3.6: страница /me — моя карточка сотрудника."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    MasterScheduleDay,
    MasterScheduleStatus,
    TelegramLinkToken,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.db.session import get_db
from app.main import app
from app.master_schedule import build_master_schedule_banner
from app.security import hash_password, verify_password
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


def _auth(user: User, role: UserRole | None = None) -> AuthUser:
    r = role or user.role
    return AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name,
        role=r,
        roles=(r,),
        master_level=user.master_level,
    )


def _seed_user(
    db,
    *,
    username: str,
    role: UserRole,
    password: str = "secret1",
    tg: int | None = None,
    notify: bool = True,
) -> User:
    u = User(
        username=username,
        password_hash=hash_password(password),
        display_name=f"User {username}",
        role=role,
        is_active=True,
        phone=None,
        telegram_chat_id=tg,
        notify_enabled=notify,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=role))
    db.commit()
    db.refresh(u)
    return u


def _client_for(db, user: User, role: UserRole | None = None) -> TestClient:
    auth = _auth(user, role)

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


@pytest.mark.parametrize(
    "role",
    [
        UserRole.MASTER,
        UserRole.ADMIN,
        UserRole.ADMIN_SUPER,
        UserRole.TECHSPEC,
        UserRole.HELPER,
        UserRole.ADMIN_SENIOR,
    ],
)
def test_me_page_opens_for_each_role(memory_db, role) -> None:
    u = _seed_user(memory_db, username=f"u_{role.value.lower()}", role=role)
    client = _client_for(memory_db, u)
    try:
        r = client.get("/me", follow_redirects=False)
        assert r.status_code == 200
        assert "Моя карточка" in r.text
        assert u.display_name in r.text
        assert u.username in r.text
        assert 'href="/me"' in r.text or "Моя карточка" in r.text
    finally:
        _clear_overrides()


def test_me_cannot_change_other_user_notify(memory_db) -> None:
    a = _seed_user(memory_db, username="a1", role=UserRole.MASTER, notify=True)
    b = _seed_user(memory_db, username="b1", role=UserRole.MASTER, notify=True, tg=999)
    client = _client_for(memory_db, a)
    try:
        # Подложенный user_id в форме игнорируется — меняется только сессионный пользователь.
        r = client.post(
            "/me/telegram/notify",
            data={"notify_enabled": "", "user_id": str(b.id)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        memory_db.refresh(a)
        memory_db.refresh(b)
        assert a.notify_enabled is False
        assert b.notify_enabled is True
        assert b.telegram_chat_id == 999
    finally:
        _clear_overrides()


def test_me_telegram_connect_disconnect_test(memory_db, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "lb_test_bot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    get_settings.cache_clear()
    u = _seed_user(memory_db, username="m_tg", role=UserRole.MASTER, tg=None)
    client = _client_for(memory_db, u)
    try:
        r = client.post("/me/telegram/connect", follow_redirects=False)
        assert r.status_code == 303
        loc = r.headers.get("location") or ""
        assert "/me?msg=tg_connect" in loc
        assert "tg_start=" in loc
        tok = memory_db.scalar(select(TelegramLinkToken).where(TelegramLinkToken.user_id == u.id))
        assert tok is not None
        assert tok.used_at is None

        # Симулируем привязку chat_id (как после вебхука).
        u.telegram_chat_id = 4242
        memory_db.commit()

        with patch("app.routes.me.send_telegram") as mock_send:
            r2 = client.post("/me/telegram/test", follow_redirects=False)
            assert r2.status_code == 303
            assert "tg_test_ok" in (r2.headers.get("location") or "")
            mock_send.assert_called_once()
            assert mock_send.call_args[0][0] == 4242

        r3 = client.post("/me/telegram/disconnect", follow_redirects=False)
        assert r3.status_code == 303
        memory_db.refresh(u)
        assert u.telegram_chat_id is None
    finally:
        _clear_overrides()


def test_me_and_home_show_schedule_banner_for_master(memory_db) -> None:
    u = _seed_user(memory_db, username="m_sch", role=UserRole.MASTER)
    memory_db.add(
        MasterScheduleDay(
            master_id=u.id,
            work_date=date(2026, 10, 10),
            status=MasterScheduleStatus.WORKING,
            time_from=None,
            time_to=None,
        )
    )
    memory_db.commit()

    banner = build_master_schedule_banner(
        memory_db, user_id=u.id, is_master_active=True, is_schedule_admin=False
    )
    assert banner is not None
    assert banner["filled_until"] == date(2026, 10, 10)

    client = _client_for(memory_db, u)
    try:
        me = client.get("/me", follow_redirects=False)
        assert me.status_code == 200
        assert 'data-lb-schedule-banner="1"' in me.text
        assert "График работы" in me.text
        assert "10.10.2026" in me.text

        home = client.get("/", follow_redirects=False)
        assert home.status_code == 200
        assert 'data-lb-schedule-banner="1"' in home.text
        assert "График работы" in home.text
    finally:
        _clear_overrides()


def test_me_password_change(memory_db) -> None:
    u = _seed_user(memory_db, username="pwd1", role=UserRole.ADMIN, password="oldpass1")
    client = _client_for(memory_db, u)
    try:
        bad = client.post(
            "/me/password",
            data={
                "current_password": "wrong",
                "new_password": "newpass1",
                "new_password_confirm": "newpass1",
            },
            follow_redirects=False,
        )
        assert "pwd_current" in (bad.headers.get("location") or "")

        ok = client.post(
            "/me/password",
            data={
                "current_password": "oldpass1",
                "new_password": "newpass1",
                "new_password_confirm": "newpass1",
            },
            follow_redirects=False,
        )
        assert "pwd_ok" in (ok.headers.get("location") or "")
        memory_db.refresh(u)
        assert verify_password("newpass1", u.password_hash)
    finally:
        _clear_overrides()


def test_me_requires_auth() -> None:
    client = TestClient(app)
    r = client.get("/me", follow_redirects=False)
    assert r.status_code in (303, 307, 401, 403)
