"""3.48: два общих чата, страница студии и галочки внутренних уведомлений."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.admin_chat import (
    CHAT_KIND_ADMIN,
    CHAT_KIND_GENERAL,
    MSG_ADMIN_CHAT_INVALID,
    MSG_ADMIN_CHAT_LINKED,
    MSG_GENERAL_CHAT_LINKED,
    MSG_GENERAL_USAGE,
    classify_chat_link_text,
    classify_general_text,
    create_admin_chat_link_token,
    get_admin_chat_target,
    try_bind_admin_chat_from_text,
)
from app.auth import AuthUser, get_current_user
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.booking_reminders import enqueue_due_booking_reminders
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    AdminChatTarget,
    Booking,
    BookingKind,
    ClientNotificationSend,
    BookingMaster,
    BookingStatus,
    Client,
    NotificationChannel,
    NotificationOutbox,
    User,
    UserRole,
    VisitMastersScope,
)
from app.db.session import get_db
from app.internal_notify_prefs import load_internal_notify_prefs, save_internal_notify_prefs
from app.main import app
from app.messenger_inbound import EVENT_BOOKING_CLIENT_CONFIRMED, handle_client_text
from app.notifications import EVENT_BOOKING_CREATED, enqueue_master_booking_notifications
from app.staff_assignment_notifications import notify_staff_assigned_on_create
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
        app.dependency_overrides.clear()


def _client(db, role: UserRole) -> TestClient:
    user = User(
        username=f"u-{role.value}",
        password_hash="x",
        display_name="Сотрудник",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    auth = AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name,
        role=role,
        roles=(role,),
    )

    def _user():
        return auth

    def _db():
        yield db

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_chat_kinds_do_not_cross_bind(db) -> None:
    assert classify_general_text("@bot /general ABC") == ("ABC", "code")
    assert classify_chat_link_text("/general")[2] == "missing"
    assert MSG_GENERAL_USAGE.startswith("Отправьте команду в виде: /general КОД")

    kept = AdminChatTarget(
        chat_kind=CHAT_KIND_ADMIN,
        channel="vk",
        chat_id=2000000001,
        linked_at=utcnow_naive(),
    )
    db.add(kept)
    db.commit()

    admin_code = create_admin_chat_link_token(db, created_by_user_id=None, chat_kind=CHAT_KIND_ADMIN)
    general_code = create_admin_chat_link_token(db, created_by_user_id=None, chat_kind=CHAT_KIND_GENERAL)
    db.commit()
    with patch("app.admin_chat.send_to_chat"):
        wrong = try_bind_admin_chat_from_text(
            db, channel="max", chat_id=42, text=f"/general {admin_code}"
        )
        ok = try_bind_admin_chat_from_text(
            db, channel="telegram", chat_id=77, text=f"/admins {admin_code}"
        )
        general = try_bind_admin_chat_from_text(
            db, channel="vk", chat_id=2000000002, text=f"/general {general_code}"
        )
    assert wrong == MSG_ADMIN_CHAT_INVALID
    assert ok == MSG_ADMIN_CHAT_LINKED
    assert general == MSG_GENERAL_CHAT_LINKED
    assert get_admin_chat_target(db, "max", chat_kind=CHAT_KIND_GENERAL) is None
    old = get_admin_chat_target(db, "vk", chat_kind=CHAT_KIND_ADMIN)
    assert old is not None and int(old.chat_id) == 2000000001
    shared = get_admin_chat_target(db, "vk", chat_kind=CHAT_KIND_GENERAL)
    assert shared is not None and int(shared.chat_id) == 2000000002


def test_studio_page_super_edits_and_other_is_readonly(db) -> None:
    super_client = _client(db, UserRole.ADMIN_SUPER)
    page = super_client.get("/studio/notifications")
    assert page.status_code == 200
    assert "Управление уведомлениями" in page.text
    assert "Редактирование доступно только суперадминистратору." not in page.text
    assert 'name="admin_client_confirm"' in page.text
    assert "disabled" not in page.text.split('name="admin_client_confirm"')[0][-40:]
    saved = super_client.post(
        "/studio/notifications/settings/save",
        data={"admin_client_confirm": "1", "general_client_confirm": "1", "dm_client_confirm": "1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    flags = load_internal_notify_prefs(db)["client_confirm"]
    assert flags == {"admin": True, "general": True, "dm": True}
    coded = super_client.post(
        "/studio/notifications/chat/code",
        data={"chat_kind": "general"},
        follow_redirects=True,
    )
    assert coded.status_code == 200
    assert "Код (24 ч)" in coded.text
    assert "/general" in coded.text

    app.dependency_overrides.clear()
    plain = _client(db, UserRole.ADMIN)
    view = plain.get("/studio/notifications")
    assert view.status_code == 200
    assert "Редактирование доступно только суперадминистратору." in view.text
    assert 'name="admin_client_confirm"' in view.text
    assert "disabled" in view.text
    denied = plain.post("/studio/notifications/settings/save", data={"admin_client_confirm": "1"})
    assert denied.status_code == 403
    denied_chat = plain.post("/studio/notifications/chat/code", data={"chat_kind": "admin"})
    assert denied_chat.status_code == 403


def test_client_pages_split(db) -> None:
    client = _client(db, UserRole.ADMIN)
    notes = client.get("/clients/notifications")
    assert notes.status_code == 200
    assert ">Уведомления<" in notes.text
    assert "Общий чат админов" not in notes.text
    assert "Раздел в разработке." not in notes.text
    assert "/clients/notifications/admin-chat/code" not in notes.text
    mail = client.get("/clients/mailings")
    assert mail.status_code == 200
    assert ">Рассылки<" in mail.text
    assert "Раздел в разработке." in mail.text
    home = client.get("/")
    assert "/studio/notifications" in home.text
    assert "Управление уведомлениями" in home.text


def _booking(db, *, planned, master: User | None = None, telegram: int | None = 3003):
    stamp = int(planned.timestamp())
    admin = User(
        username=f"maker-{stamp}-{telegram or 0}",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(
        name="Катя",
        phone=f"+7999{stamp % 10000000:07d}",
        is_confirmed=True,
        telegram_chat_id=telegram,
    )
    db.add_all([admin, client])
    db.commit()
    booking = Booking(
        created_by_user_id=admin.id,
        client_id=client.id,
        planned_date=planned,
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.commit()
    if master is not None:
        db.add(BookingMaster(booking_id=booking.id, master_id=master.id))
        db.commit()
    if telegram is not None:
        db.add(
            ClientNotificationSend(
                client_id=client.id,
                booking_id=booking.id,
                channel="telegram",
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=int(telegram),
                sent_at=utcnow_naive(),
            )
        )
        db.commit()
    return client, booking


def test_reply_and_booking_and_staff_flags(db) -> None:
    db.add(
        AdminChatTarget(chat_kind="admin", channel="vk", chat_id=111, title="A", linked_at=utcnow_naive())
    )
    db.add(
        AdminChatTarget(chat_kind="general", channel="max", chat_id=222, title="G", linked_at=utcnow_naive())
    )
    master = User(
        username="masha",
        password_hash="x",
        display_name="Маша",
        role=UserRole.MASTER,
        is_active=True,
        vk_user_id=777,
        notify_bookings=True,
        notify_visit=True,
    )
    db.add(master)
    db.commit()
    client, booking = _booking(db, planned=utcnow_naive() + timedelta(hours=5), master=master)
    save_internal_notify_prefs(
        db,
        {
            "admin_client_confirm": "1",
            "general_client_confirm": "1",
            "dm_client_confirm": "1",
            "dm_booking_created": "1",
            "admin_booking_created": "1",
            "general_booking_reminder": "1",
            "general_staff_visit": "1",
        },
        user_id=None,
    )
    db.commit()

    handle_client_text(db, channel="telegram", messenger_id=3003, text="1")
    db.commit()
    rows = list(
        db.scalars(
            select(NotificationOutbox).where(NotificationOutbox.event_type == EVENT_BOOKING_CLIENT_CONFIRMED)
        ).all()
    )
    kinds = {r.target_kind for r in rows}
    assert {"admin_chat", "general_chat", "user"} <= kinds
    assert any("Клиентка подтвердила запись" in (r.body or "") for r in rows)

    enqueue_master_booking_notifications(db, booking, EVENT_BOOKING_CREATED)
    db.commit()
    created = list(
        db.scalars(
            select(NotificationOutbox).where(NotificationOutbox.event_type == EVENT_BOOKING_CREATED)
        ).all()
    )
    assert any(r.target_kind == "admin_chat" and "Мастера: Маша" in (r.body or "") for r in created)
    assert any(r.target_kind == "user" for r in created)
    assert all(r.target_kind != "general_chat" for r in created)

    soon = utcnow_naive() + timedelta(hours=24) - timedelta(minutes=5)
    _booking(db, planned=soon, master=master, telegram=None)
    stats = enqueue_due_booking_reminders(db, now=utcnow_naive())
    db.commit()
    reminders = list(
        db.scalars(
            select(NotificationOutbox).where(NotificationOutbox.event_type == "booking_reminder")
        ).all()
    )
    assert stats["enqueued"] >= 1
    assert reminders
    assert all(r.target_kind == "general_chat" for r in reminders)
    assert any(r.dedupe_key.startswith("br_chat:general:") for r in reminders)
    assert any("Мастера:" in (r.body or "") for r in reminders)

    with patch("app.staff_assignment_notifications.process_outbox"):
        notify_staff_assigned_on_create(
            db, entity_type="visit", entity_id=15, user_ids=[master.id], client_name="Катя"
        )
    staff_rows = list(
        db.scalars(
            select(NotificationOutbox).where(NotificationOutbox.event_type == "staff_assignment_created")
        ).all()
    )
    assert staff_rows
    assert all(r.target_kind == "general_chat" for r in staff_rows)
    assert any("Сотрудники: Маша" in (r.body or "") for r in staff_rows)
