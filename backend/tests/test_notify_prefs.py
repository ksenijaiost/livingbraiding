"""3.14: типы уведомлений на /me и фильтры отправки."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import NotificationOutbox, User, UserRole, UserRoleAssignment
from app.db.session import get_db
from app.main import app
from app.notify_prefs import (
    NOTIFY_TYPE_KEYS,
    apply_notify_prefs_from_legacy_flag,
    notify_prefs_for_ui,
    notify_type_available,
)
from app.security import hash_password
from app.settings import get_settings
from app.staff_assignment_notifications import notify_staff_assigned_on_create


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


def _seed(db, *, username: str, role: UserRole, tg: int | None = 1, notify: bool = True) -> User:
    u = User(
        username=username,
        password_hash=hash_password("secret1"),
        display_name=username,
        role=role,
        is_active=True,
        telegram_chat_id=tg,
        notify_enabled=notify,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=role))
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [role], enabled=notify)
    db.commit()
    db.refresh(u)
    return u


def _client_for(db, user: User) -> TestClient:
    auth = _auth(user)

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


def _clear() -> None:
    app.dependency_overrides.clear()


def test_helper_role_availability() -> None:
    roles = [UserRole.HELPER]
    assert notify_type_available(roles, "hourly_work")
    assert notify_type_available(roles, "work_plan")
    assert notify_type_available(roles, "visit")
    assert not notify_type_available(roles, "work")
    assert not notify_type_available(roles, "product_sale")
    assert not notify_type_available(roles, "bookings")


def test_master_role_availability() -> None:
    roles = [UserRole.MASTER]
    for key in NOTIFY_TYPE_KEYS:
        assert notify_type_available(roles, key)


def test_me_helper_checkboxes_disabled(memory_db) -> None:
    u = _seed(memory_db, username="h1", role=UserRole.HELPER)
    client = _client_for(memory_db, u)
    try:
        r = client.get("/me", follow_redirects=False)
        assert r.status_code == 200
        assert 'name="notify_telegram_hourly_work"' in r.text
        assert 'name="notify_telegram_work_plan"' in r.text
        assert 'name="notify_telegram_work"' not in r.text
        assert 'name="notify_telegram_product_sale"' not in r.text
        assert "Недоступно для ваших ролей" in r.text
        assert "Для каждого канала можно выбрать свои типы уведомлений" in r.text
    finally:
        _clear()


def test_me_master_almost_all_active(memory_db) -> None:
    u = _seed(memory_db, username="m1", role=UserRole.MASTER)
    cols = notify_prefs_for_ui(u, [UserRole.MASTER])
    assert all(c["available"] for c in cols)
    client = _client_for(memory_db, u)
    try:
        r = client.get("/me", follow_redirects=False)
        assert r.status_code == 200
        for key in NOTIFY_TYPE_KEYS:
            assert f'name="notify_telegram_{key}"' in r.text
            assert f'name="notify_vk_{key}"' not in r.text
    finally:
        _clear()


def test_post_does_not_enable_disabled_types(memory_db) -> None:
    u = _seed(memory_db, username="h1", role=UserRole.HELPER)
    assert u.notify_work is False
    client = _client_for(memory_db, u)
    try:
        r = client.post(
            "/me/notify",
            data={
                "notify_channel_telegram": "1",
                "notify_telegram_hourly_work": "1",
                "notify_telegram_work_plan": "1",
                "notify_telegram_work": "1",  # недоступно HELPER — игнорируется
                "notify_telegram_product_sale": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        memory_db.refresh(u)
        assert u.notify_hourly_work is True
        assert u.notify_work_plan is True
        assert u.notify_work is False
        assert u.notify_product_sale is False
    finally:
        _clear()


def test_visit_pref_off_skips_staff_assignment(memory_db) -> None:
    from sqlalchemy import func, select

    from app.notify_prefs import set_notify_pref

    u = _seed(memory_db, username="m1", role=UserRole.MASTER, tg=55)
    set_notify_pref(u, "visit", False)
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_staff_assigned_on_create(
            memory_db, entity_type="visit", entity_id=9, user_ids=[u.id]
        )
    assert int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0) == 0


def test_bookings_pref_off_skips_booking_notify(memory_db) -> None:
    from datetime import timedelta

    from app.booking_notifications import enqueue_master_booking_notifications
    from app.db.models import Booking, BookingKind, BookingMaster, BookingStatus, Client, VisitMastersScope
    from app.time_utils import utcnow_naive

    from app.notify_prefs import set_notify_pref

    u = _seed(memory_db, username="m1", role=UserRole.MASTER, tg=77)
    set_notify_pref(u, "bookings", False)
    c = Client(name="K", phone="+79991112233", is_confirmed=True)
    memory_db.add(c)
    memory_db.flush()
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=utcnow_naive() + timedelta(hours=48),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    memory_db.add(b)
    memory_db.flush()
    memory_db.add(BookingMaster(booking_id=b.id, master_id=u.id))
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        rows = enqueue_master_booking_notifications(memory_db, b, "booking_created")
    assert rows == []


def test_legacy_migration_defaults(memory_db) -> None:
    """Симуляция миграции: notify_enabled True/False → prefs по ролям."""
    master_on = _seed(memory_db, username="m_on", role=UserRole.MASTER, notify=True)
    helper_off = _seed(memory_db, username="h_off", role=UserRole.HELPER, notify=False)
    assert master_on.notify_bookings is True
    assert master_on.notify_work is True
    assert master_on.notify_enabled is True
    assert helper_off.notify_hourly_work is False
    assert helper_off.notify_work_plan is False
    assert helper_off.notify_enabled is False

    # HELPER + notify True: почасовая/план/визит вкл, работа/продажа выкл
    helper_on = User(
        username="h_on",
        password_hash="x",
        display_name="h",
        role=UserRole.HELPER,
        is_active=True,
        notify_enabled=True,
    )
    memory_db.add(helper_on)
    memory_db.flush()
    memory_db.add(UserRoleAssignment(user_id=helper_on.id, role=UserRole.HELPER))
    memory_db.flush()
    apply_notify_prefs_from_legacy_flag(helper_on, [UserRole.HELPER], enabled=True)
    memory_db.commit()
    memory_db.refresh(helper_on)
    assert helper_on.notify_hourly_work is True
    assert helper_on.notify_work_plan is True
    assert helper_on.notify_visit is True
    assert helper_on.notify_work is False
    assert helper_on.notify_product_sale is False
    assert helper_on.notify_bookings is False


def test_admin_staff_edit_shows_types_summary(memory_db) -> None:
    admin = _seed(memory_db, username="super1", role=UserRole.ADMIN_SUPER)
    master = _seed(memory_db, username="masha", role=UserRole.MASTER)
    client = _client_for(memory_db, admin)
    # ADMIN_SUPER for route
    auth = _auth(admin, UserRole.ADMIN_SUPER)

    def _override_user():
        return auth

    app.dependency_overrides[get_current_user] = _override_user
    try:
        r = client.get(f"/admin/settings/staff/{master.id}/edit", follow_redirects=False)
        assert r.status_code == 200
        assert "Брони" in r.text
        assert "Визит" in r.text
        assert "VK" in r.text
        assert "Max" in r.text
        assert "Telegram" in r.text
        assert "Включить все" not in r.text
        assert ">Подключить<" not in r.text
    finally:
        _clear()


def test_copy_shared_prefs_to_all_channels(memory_db) -> None:
    from sqlalchemy import func, select

    from app.db.models import UserNotifyChannelPref
    from app.notify_prefs import NOTIFY_CHANNEL_IDS, copy_shared_notify_prefs_to_channels, get_channel_pref

    u = User(
        username="copy1",
        password_hash="x",
        display_name="copy",
        role=UserRole.MASTER,
        is_active=True,
        notify_enabled=True,
        notify_bookings=True,
        notify_visit=False,
        notify_work=True,
        notify_hourly_work=False,
        notify_product_sale=True,
        notify_consultation=False,
        notify_work_plan=True,
    )
    memory_db.add(u)
    memory_db.commit()
    copy_shared_notify_prefs_to_channels(memory_db.connection())
    memory_db.commit()
    memory_db.expire_all()
    u = memory_db.get(User, u.id)
    assert u is not None
    for ch in NOTIFY_CHANNEL_IDS:
        assert get_channel_pref(u, "bookings", ch) is True
        assert get_channel_pref(u, "visit", ch) is False
        assert get_channel_pref(u, "work", ch) is True
        assert get_channel_pref(u, "hourly_work", ch) is False
    n = int(memory_db.scalar(select(func.count()).select_from(UserNotifyChannelPref)) or 0)
    assert n == 7 * 3
    copy_shared_notify_prefs_to_channels(memory_db.connection())
    memory_db.commit()
    n2 = int(memory_db.scalar(select(func.count()).select_from(UserNotifyChannelPref)) or 0)
    assert n2 == n


def test_save_one_channel_does_not_touch_others(memory_db) -> None:
    from app.notify_prefs import get_channel_pref

    u = _seed(memory_db, username="m_ch", role=UserRole.MASTER, tg=11)
    u.vk_user_id = 22
    u.max_user_id = 33
    memory_db.commit()
    client = _client_for(memory_db, u)
    try:
        r = client.post(
            "/me/notify",
            data={
                "notify_channel_vk": "1",
                "notify_vk_visit": "1",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        memory_db.refresh(u)
        assert get_channel_pref(u, "bookings", "vk") is False
        assert get_channel_pref(u, "visit", "vk") is True
        assert get_channel_pref(u, "bookings", "max") is True
        assert get_channel_pref(u, "bookings", "telegram") is True
        assert get_channel_pref(u, "visit", "telegram") is True
    finally:
        _clear()


def test_disabled_channel_skips_outbox_other_channel_sends(memory_db) -> None:
    from sqlalchemy import select

    from app.db.models import NotificationChannel
    from app.notify_prefs import set_notify_pref

    u = _seed(memory_db, username="m_out", role=UserRole.MASTER, tg=77)
    u.vk_user_id = 88
    set_notify_pref(u, "visit", False, "vk")
    set_notify_pref(u, "visit", True, "telegram")
    memory_db.commit()
    with patch("app.notifications.send_telegram"), patch("app.notifications.send_vk"):
        notify_staff_assigned_on_create(
            memory_db, entity_type="visit", entity_id=9, user_ids=[u.id]
        )
    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    assert [row.channel for row in rows] == [NotificationChannel.TELEGRAM]
