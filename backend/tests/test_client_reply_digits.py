"""3.34: настраиваемые цифры ответов и {{reply_hint}}."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.client_notifications import DEFAULT_BEFORE_TEMPLATE, render_client_notify_template
from app.client_reply_digits import (
    ACTION_CONFIRM,
    DigitConflictError,
    LEGACY_REPLY_HINT_LINE,
    build_reply_hint,
    ensure_default_reply_digits,
    migrate_replace_legacy_reply_hint,
    reply_hint_system_values,
    save_reply_digits_from_form,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    AdminChatTarget,
    Booking,
    BookingKind,
    BookingStatus,
    BookingStatusNotifyRule,
    Client,
    ClientNotificationRule,
    ClientNotificationRuleKind,
    ClientNotificationSend,
    ClientReplyDigitAction,
    NotificationOutbox,
    User,
    UserRole,
    VisitMastersScope,
)
from app.messenger_inbound import (
    EVENT_CLIENT_CONTACT_REQUEST,
    MSG_ALREADY_CONFIRMED,
    MSG_CONFIRMED,
    handle_client_text,
)
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_client(db, *, with_send: bool = True):
    ensure_default_reply_digits(db)
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(
        name="Катя",
        is_confirmed=True,
        vk_user_id=1001,
        max_user_id=2002,
        telegram_chat_id=3003,
    )
    db.add_all([user, client])
    db.commit()
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=utcnow_naive() + timedelta(hours=20),
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.commit()
    if with_send:
        db.add(
            ClientNotificationSend(
                client_id=client.id,
                booking_id=booking.id,
                channel="vk",
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=1001,
                sent_at=utcnow_naive(),
            )
        )
        db.commit()
    return user, client, booking


def test_swap_1_and_4_changes_actions(memory_db) -> None:
    _user, _client, booking = _seed_client(memory_db)
    ensure_default_reply_digits(memory_db)
    save_reply_digits_from_form(
        memory_db,
        actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
        digits=["4", "2", "3", "1", "5"],
        enabled_flags=["1", "1", "1", "1", "1"],
        labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
    )
    memory_db.commit()

    ans4 = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="4")
    memory_db.commit()
    memory_db.refresh(booking)
    assert ans4 == MSG_CONFIRMED
    assert booking.client_confirmed_at is not None

    ans1 = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1")
    memory_db.commit()
    assert "Администратор скоро напишет" in (ans1 or "")

    hint = build_reply_hint(memory_db)
    assert "4 — подтвердить" in hint
    assert "1 — написать администратору" in hint
    assert hint.startswith("Ответьте:")


def test_disabled_action_shows_hint_and_drops_from_reply_hint(memory_db) -> None:
    _seed_client(memory_db)
    save_reply_digits_from_form(
        memory_db,
        actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
        digits=["1", "2", "3", "4", "5"],
        enabled_flags=["0", "1", "1", "1", "1"],
        labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
    )
    memory_db.commit()
    hint = build_reply_hint(memory_db)
    assert "подтвердить" not in hint
    assert "2 — перенести" in hint
    ans = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1")
    assert ans == hint


def test_reply_hint_variable_in_template(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    memory_db.commit()
    client = Client(name="Катя", is_confirmed=True)
    memory_db.add(client)
    memory_db.commit()
    booking = type(
        "B",
        (),
        {
            "planned_date": utcnow_naive() + timedelta(hours=5),
            "planned_services": [],
            "planned_service": None,
            "masters": [],
        },
    )()
    text = render_client_notify_template(
        "Привет, {{name}}. {{reply_hint}}",
        booking=booking,  # type: ignore[arg-type]
        client=client,
        tz_name="UTC",
        db=memory_db,
    )
    assert "Привет, Катя." in text
    assert "{{reply_hint}}" not in text
    assert "1 — подтвердить" in text
    assert DEFAULT_BEFORE_TEMPLATE.endswith("{{reply_hint}}")


def test_digit_conflict_raises_field_errors(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    with pytest.raises(DigitConflictError) as caught:
        save_reply_digits_from_form(
            memory_db,
            actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
            digits=["1", "1", "3", "4", "5"],
            enabled_flags=["1", "1", "1", "1", "1"],
            labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
        )
    assert ACTION_CONFIRM in caught.value.field_errors
    assert "reschedule" in caught.value.field_errors


def test_migration_replaces_exact_legacy_line(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    rule = ClientNotificationRule(
        kind=ClientNotificationRuleKind.BEFORE_BOOKING,
        hours=24,
        template=f"Добрый день.\n\n{LEGACY_REPLY_HINT_LINE}",
        position=0,
        is_enabled=True,
    )
    custom = ClientNotificationRule(
        kind=ClientNotificationRuleKind.BEFORE_BOOKING,
        hours=2,
        template="Ответьте: 1 — ок, 2 — нет",
        position=1,
        is_enabled=True,
    )
    memory_db.add_all([rule, custom])
    memory_db.commit()
    report = migrate_replace_legacy_reply_hint(memory_db.connection())
    memory_db.commit()
    memory_db.refresh(rule)
    memory_db.refresh(custom)
    assert "{{reply_hint}}" in rule.template
    assert LEGACY_REPLY_HINT_LINE not in rule.template
    assert int(rule.id) in report["replaced"]
    assert "Ответьте: 1 — ок" in custom.template
    assert int(custom.id) in report["manual"]


def test_migration_active_status_uses_reply_hint_no_confirm(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    active = BookingStatusNotifyRule(
        status=BookingStatus.ACTIVE.value,
        template=f"Подтверждена.\n\n{LEGACY_REPLY_HINT_LINE}",
        position=0,
        is_enabled=True,
    )
    other = BookingStatusNotifyRule(
        status=BookingStatus.PENDING_CONFIRMATION.value,
        template=f"Ожидает.\n\n{LEGACY_REPLY_HINT_LINE}",
        position=1,
        is_enabled=True,
    )
    memory_db.add_all([active, other])
    memory_db.commit()
    report = migrate_replace_legacy_reply_hint(memory_db.connection())
    memory_db.commit()
    memory_db.refresh(active)
    memory_db.refresh(other)
    assert active.template.endswith("{{reply_hint_no_confirm}}")
    assert LEGACY_REPLY_HINT_LINE not in active.template
    assert other.template.endswith("{{reply_hint}}")
    assert "{{reply_hint_no_confirm}}" not in other.template
    assert int(active.id) in report["replaced"]
    assert int(other.id) in report["replaced"]


def _save_digits(db, enabled: str) -> None:
    save_reply_digits_from_form(
        db,
        actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
        digits=["1", "2", "3", "4", "5"],
        enabled_flags=list(enabled),
        labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
    )
    db.commit()


def test_three_reply_hints_follow_enabled_rows(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    memory_db.commit()
    values = reply_hint_system_values(memory_db)
    assert values["reply_hint"] == (
        "Ответьте: 1 — подтвердить, 2 — перенести, 3 — отменить, "
        "4 — написать администратору, 5 — как добраться."
    )
    assert values["reply_hint_no_confirm"] == (
        "Ответьте: 2 — перенести, 3 — отменить, 4 — написать администратору, 5 — как добраться."
    )
    assert values["reply_hint_general"] == "Ответьте: 4 — написать администратору, 5 — как добраться."

    _save_digits(memory_db, "01110")
    values = reply_hint_system_values(memory_db)
    assert "подтвердить" not in values["reply_hint"]
    assert values["reply_hint"] == values["reply_hint_no_confirm"]
    assert values["reply_hint_general"] == "Ответьте: 4 — написать администратору."
    assert "\n" not in values["reply_hint_general"]

    _save_digits(memory_db, "01100")
    values = reply_hint_system_values(memory_db)
    assert values["reply_hint_general"] == ""
    assert "2 — перенести" in values["reply_hint"]
    assert "4 — написать администратору" not in values["reply_hint"]
    assert "5 — как добраться" not in values["reply_hint_no_confirm"]

    client = Client(name="Катя", is_confirmed=True)
    memory_db.add(client)
    memory_db.commit()
    booking = type(
        "B",
        (),
        {
            "planned_date": utcnow_naive(),
            "planned_services": [],
            "planned_service": None,
            "masters": [],
        },
    )()
    text = render_client_notify_template(
        "После, {{name}}.\n{{reply_hint_general}}\n{{reply_hint}}",
        booking=booking,  # type: ignore[arg-type]
        client=client,
        tz_name="UTC",
        db=memory_db,
        substitute_system=False,
        system_keys=frozenset({"name", "reply_hint_general"}),
    )
    assert text == "После, Катя.\n\n{{reply_hint}}"


def test_contact_and_directions_without_booking_confirm_ignored(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    client = Client(name="Катя", is_confirmed=True, vk_user_id=1001)
    memory_db.add(client)
    memory_db.add(AdminChatTarget(channel="vk", chat_id=77, title="admins", linked_at=utcnow_naive()))
    memory_db.commit()

    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1") is None
    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="2") is None
    assert handle_client_text(memory_db, channel="vk", messenger_id=1001, text="3") is None

    four = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="4")
    memory_db.commit()
    assert four == "Администратор скоро напишет вам здесь"
    notices = list(
        memory_db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == EVENT_CLIENT_CONTACT_REQUEST
            )
        ).all()
    )
    assert len(notices) == 1
    assert notices[0].booking_id is None

    five = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="5")
    assert five is not None and "Как добраться" in five


def test_shared_account_general_digits_without_booking(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    shared = 515151
    staff = User(
        username="shared",
        password_hash="x",
        display_name="Маша",
        role=UserRole.MASTER,
        is_active=True,
        max_user_id=shared,
    )
    client = Client(name="Оля", is_confirmed=True, max_user_id=shared)
    memory_db.add_all([staff, client])
    memory_db.add(AdminChatTarget(channel="max", chat_id=88, title="admins", linked_at=utcnow_naive()))
    memory_db.commit()

    assert handle_client_text(memory_db, channel="max", messenger_id=shared, text="1") is None

    four = handle_client_text(memory_db, channel="max", messenger_id=shared, text="4")
    memory_db.commit()
    assert four == "Напишите нам: {{studio_max}}"
    notices = list(
        memory_db.scalars(
            select(NotificationOutbox).where(
                NotificationOutbox.event_type == EVENT_CLIENT_CONTACT_REQUEST
            )
        ).all()
    )
    assert len(notices) == 1
    assert notices[0].booking_id is None

    five = handle_client_text(memory_db, channel="max", messenger_id=shared, text="5")
    assert five is not None and "Как добраться" in five
