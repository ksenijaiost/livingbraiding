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
        assert 'name="notify_hourly_work"' in r.text
        assert 'name="notify_work_plan"' in r.text
        assert 'name="notify_work"' not in r.text
        assert 'name="notify_product_sale"' not in r.text
        assert "Недоступно для ваших ролей" in r.text
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
            assert f'name="notify_{key}"' in r.text
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
                "notify_hourly_work": "1",
                "notify_work_plan": "1",
                "notify_work": "1",  # недоступно HELPER — игнорируется
                "notify_product_sale": "1",
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

    from app.notify_prefs import sync_notify_enabled_from_prefs

    u = _seed(memory_db, username="m1", role=UserRole.MASTER, tg=55)
    u.notify_visit = False
    sync_notify_enabled_from_prefs(u)
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

    u = _seed(memory_db, username="m1", role=UserRole.MASTER, tg=77)
    u.notify_bookings = False
    from app.notify_prefs import sync_notify_enabled_from_prefs

    sync_notify_enabled_from_prefs(u)
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
        assert "Типы уведомлений:" in r.text
        assert "Брони" in r.text
        assert "Визит" in r.text
    finally:
        _clear()
